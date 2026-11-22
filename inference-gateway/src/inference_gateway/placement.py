"""Deterministic head choice for one queued request (release 0.4.0; 0.4.3 order and per-model limits).

Replaces "first free head in rowid order". Called inside the broker's admission
transaction (BEGIN IMMEDIATE), so the view below is one consistent snapshot and
every lease it grants is updated in place before the next request is placed.

Eligible: healthy and fresh (as 0.3.0), authorized, the model qualified (as 0.3.0),
not suspended or held, a free slot. Then, in this order:
  1. loaded     the model is loaded (fresh /api/ps) or in use by a lease on the head;
                fewest active leases, then least recently assigned, then resource_id
  2. fits       not loaded, fits in free memory without unloading anything;
                most free memory, then least recently assigned, then resource_id
  3. evicts     loading may unload idle models only (never one an active lease uses);
                fewest active leases, then least recently assigned, then resource_id
With unknown memory or stale /api/ps data the head is "loaded unknown": only tier 3,
and only when every active lease on it uses the same model.

0.4.3, placement_policy.strategy:
  least_busy (default)  the head with the fewest active leases per slot wins; the tier above
                        only breaks ties, and a head that must evict comes after every head
                        that need not. Load spreads over the heads instead of piling on the
                        one head that has the model loaded.
  loaded_first          the 0.4.0-0.4.2 order: tier first, load only inside a tier.
0.4.3, per-model limit: a head takes at most N leases of one model, where N is
placement_policy.model_parallel[model], else 1 when the model's family (from the head's
/api/tags) is in placement_policy.serial_families (Ollama runs those one call at a time
whatever OLLAMA_NUM_PARALLEL says), else the head's slots.
0.4.4: N comes from model_capacity.effective: declared (Arena) and measured (Gateway) values
that match the head's live runtime version and the installed digest, else the conservative
default (serial families 1 below placement_policy.parallel_runtime_min or without evidence).
"""
import json
from .recovery import suspension_reason
from . import model_capacity

GIB=1024**3
TIERS={1:'loaded',2:'fits',3:'evicts'}


class View:
    def __init__(self,db,now,resources,policy,suspensions):
        self.now=now;self.policy=policy;self.suspensions=suspensions
        self.resources={r['resource_id']:r for r in resources}
        self.capacity={r['resource_id']:dict(r) for r in db.execute('SELECT * FROM capacity_resource_capacity')}
        self.runtime={r['resource_id']:dict(r) for r in db.execute('SELECT * FROM capacity_inventory_runtime')}
        inventory=[dict(r) for r in db.execute('SELECT resource_id,models,runtime_version FROM capacity_inventory')]
        self.digests={r['resource_id']:json.loads(r['models']) for r in inventory}
        self.versions={r['resource_id']:model_capacity.live_version(r['runtime_version']) for r in inventory}
        self.evidence=model_capacity.load(db);self._limits={}
        self.leases={}
        for row in db.execute("SELECT resource_id,model FROM capacity_requests WHERE status IN ('leased','cancel_requested') AND resource_id IS NOT NULL"):
            self.leases.setdefault(row['resource_id'],[]).append(row['model'])
        # Largest resident size actually observed for a model build on any head.
        self.learned={}
        for rid,row in self.runtime.items():
            for item in self.loaded(rid) or []:
                key=(item.get('name'),item.get('digest'))
                if isinstance(item.get('size'),int):self.learned[key]=max(self.learned.get(key,0),item['size'])

    # -- facts per head
    def slots(self,rid):return int((self.capacity.get(rid) or {}).get('slots') or 1)
    def memory(self,rid):
        value=(self.capacity.get(rid) or {}).get('memory_gb')
        return int(value*GIB) if value else None
    def last_assigned(self,rid):return (self.capacity.get(rid) or {}).get('last_assigned') or 0
    def active(self,rid):return len(self.leases.get(rid,[]))
    def families(self,rid,model):
        """The model's families as this head's /api/tags reported them (0.4.3), else empty."""
        value=json.loads((self.runtime.get(rid) or {}).get('families') or '{}').get(model)
        return set(value) if isinstance(value,list) else set()
    def effective_limit(self,rid,model):
        """0.4.4: the per-model limit with its source (model_capacity.effective), cached for this pass."""
        key=(rid,model)
        if key not in self._limits:
            rows=self.evidence.get(key,{})
            self._limits[key]=model_capacity.effective(model,slots=self.slots(rid),families=self.families(rid,model),
                runtime_version=self.versions.get(rid),digest=self.digests.get(rid,{}).get(model),
                declared=rows.get('declared'),measured=rows.get('measured'),policy=self.policy)
        return self._limits[key]
    def model_limit(self,rid,model):
        """Leases of this model one head may hold at once (None: only the head's slots limit it)."""
        limit=self.effective_limit(rid,model)
        return None if limit['source']=='slots' else limit['limit']
    def loaded(self,rid):
        row=self.runtime.get(rid)
        if not row or row['loaded'] is None or not row['loaded_at'] or row['loaded_at']<=self.now-self.policy['loaded_fresh_seconds']:return None
        return json.loads(row['loaded'])
    def size(self,rid,model):
        digest=self.digests.get(rid,{}).get(model)
        learned=self.learned.get((model,digest))
        tags=json.loads((self.runtime.get(rid) or {}).get('sizes') or '{}').get(model)
        estimate=int(tags*self.policy['model_size_factor']) if isinstance(tags,int) else None
        values=[v for v in (learned,estimate) if v]
        return max(values) if values else None

    def tier(self,rid,model):
        """(tier, sort key) or None when this head must not take this model now."""
        if self.active(rid)>=self.slots(rid):return None
        limit=self.model_limit(rid,model)
        if limit is not None and self.leases.get(rid,[]).count(model)>=limit:return None
        in_use=self.leases.get(rid,[]);loaded=self.loaded(rid);memory=self.memory(rid);last=self.last_assigned(rid)
        names={item.get('name') for item in loaded or []}
        if model in in_use or model in names:return 1,(self.active(rid),last,rid)
        if loaded is None or memory is None:
            # Loaded unknown: only a head whose active leases all use this model (or none).
            return (3,(self.active(rid),last,rid)) if not set(in_use)-{model} else None
        need=self.size(rid,model)
        sizes={item.get('name'):item.get('size') for item in loaded}
        committed=[(m,sizes.get(m) if isinstance(sizes.get(m),int) else self.size(rid,m)) for m in names|set(in_use)]
        if need is None or any(v is None for _,v in committed):
            return (3,(self.active(rid),last,rid)) if not set(in_use)-{model} else None
        used=sum(v for _,v in committed)
        if used+need<=memory:return 2,(-(memory-used),last,rid)
        busy=sum(v for m,v in committed if m in in_use)
        if busy+need<=memory:return 3,(self.active(rid),last,rid)
        return None # loading would unload a model an active lease is using

    def choose(self,request,candidates,operation):
        best=None
        for rid in candidates:
            digest=self.digests.get(rid,{}).get(request['model'])
            if suspension_reason(self.suspensions,rid,request['model'],digest,operation):continue
            placed=self.tier(rid,request['model'])
            if not placed:continue
            rank=self.rank(rid,placed)
            if best is None or rank<best[0]:best=(rank,rid,placed[0])
        return (best[1],best[2]) if best else None

    def rank(self,rid,placed):
        """Sort key of an eligible head: smallest wins (see the module docstring)."""
        if self.policy.get('strategy','least_busy')=='loaded_first':return placed
        return (placed[0]==3,self.active(rid)/self.slots(rid),placed[0],placed[1])

    def assign(self,rid,model):
        self.leases.setdefault(rid,[]).append(model)
        self.capacity.setdefault(rid,{'resource_id':rid,'slots':1,'memory_gb':None})['last_assigned']=self.now
