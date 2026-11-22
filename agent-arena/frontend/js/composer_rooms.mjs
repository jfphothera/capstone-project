// Fitted room layouts for the scene composer. The geometry was measured on the clean source
// plates (assets/scene-kit-sources/2026-11-01/plates), not taken from kit profiles: the kit's
// base.png stamps generic doors, a whiteboard, plinths and a floor sign case that do not match
// the painted rooms. All twenty plates share one skeleton (alcoves, pillars, ribbed bay, floor
// line, trim ledge), checked by overlaying these lines on every plate. Sockets say WHERE a kind
// of thing may go; theme pools say WHAT may fill it; the composer fills them from a seed.
// Each room's doors, wall panels and ticker style come from the kits' own room profiles.

export const SKELETON={
  // The painted door alcoves: left x200-385, right x1152-1336, y40-218, floor line y~222.
  doors:{b:{center:292,sill:226},a:{center:1244,sill:226}},
  tickerY:4,tickerScale:.6,                       // on the top beam above each alcove
  tickerScale2:.42,                               // ticker-signs-v2 housings (512 x 128)
  lightSwitch:[1112,150],                         // the wall switch beside Door A (Door B has the signal tower)
  // Sizes, set against the doors (opening 152 px at the sill): people stand about two thirds of it.
  scale:{people:1.35,props:1.7,furniture:1.3,plants:1.12},
  wallSeam:.37,                                   // side wall foot: x moves .37 px inward per px of depth
  wallFeet:[580,665],                             // wall-depth benches stay on the straight side wall (it angles in below ~680)
  signal:[432,178,62],                            // signal tower beside Door B: x, feet y, height
  bay:{x:[566,976],y:[62,150]},                   // ribbed centre bay between the pillars
  lighting:{pillarCenters:[493,1043],pillarFeet:160,pillarHeight:[36,42],hangingOffset:[150,180],hangingFeet:54,hangingHeight:[30,38]},
  backFloor:{x:[560,982],feet:[258,266]},         // against the back wall, between the pillars
  backRow:[['bench'],['bench','bench'],['bench','unit'],['unit','bench']],
  // Between each alcove and its pillar: debris collects by Door B; crates and the drone's dock by Door A.
  debrisPile:{x:[402,456],feet:254},cratePile:{x:[1090,1140],feet:252},dock:[1176,318],
  beside:[{x:[404,452],feet:256},{x:[1086,1136],feet:256}],   // labs put plants or instruments here
  corners:[{x:[108,150],feet:[300,322]},{x:[1386,1428],feet:[300,322]}],
  sideWalls:[{side:'left',x:-60,top:[372,560]},{side:'right',x:1220,top:[372,560]}],
  sideWallPlant:{left:[70,112],right:[1424,1466]},
  floorEdge:{left:[150,250,-.33],right:[1386,250,.33]},   // side walls' foot lines on the plate: x at y=250, x per px of depth
  frontCorners:[{x:[214,236],feet:908},{x:[1300,1322],feet:908}],   // clear of the angled front walls (~x 180 / 1356 at the bottom)
  // Front trim: top ledge face y~930-941, segment seams x~454/655/852/1071.
  bulbs:{feet:938,height:30,spacing:34,groups:{pass:[462,648],fail:[858,1064]}},
  inspectorY:902,aisle:541,staffLaneY:300,
  stand:{x:[262,470],y:[480,858]},work:{x:[600,1210],y:[478,858]},
  screens:{height:84},                            // labs: the two result screens, mounted in the bay
};

export const THEMES={
  industrial:{
    bench:['left-assembly-1','left-assembly-2','left-assembly-4','right-assembly-1'],
    floorUnit:['utilities/server-drives','utilities/charging-rack','utilities/piston-compressor','utilities/gear-cabinet','utilities/coil-regulator','utilities/reel-archive'],
    wallSign:['signs/hazard-lightbox','signs/riveted-plaque','signs/split-flap'],
    wallLamp:['lights/caged-beacon','lights/brass-utility','lights/bulkhead-ring','lights/retro-sconce'],
    hangingLamp:['lights/grille-strip','lights/dome-lamp'],
    floorPlant:['plants/snake-plant','plants/rubber-plant','plants/olive-palm','plants/steel-fern','plants/cactus-garden'],
    sidePieces:['left-assembly-5','right-assembly-5'],
    // Wall-depth furniture (sidewall-v2): a surface along each side wall and one piece on it.
    wallSurface:{left:['left/copper-wall-bench','left/wall-table-fit-v2'],right:['right/wall-table-fit-v2','right/burgundy-wall-cabinet']},
    wallProp:{left:['left/crt-fit-v2','left/wall-reel-recorder','left/wall-task-lamp','left/wall-flip-clock'],right:['right/crt-fit-v2','right/wall-cassette-deck','right/wall-inspection-lamp','right/wall-flip-clock']},
  },
  tech:{
    bench:['left-assembly-1','left-assembly-3','right-assembly-1','right-assembly-3'],
    floorUnit:['utilities/server-drives','utilities/charging-rack','utilities/coil-regulator','utilities/reel-archive','terminals/server-terminal','terminals/amber-operator','terminals/round-diagnostic'],
    wallSign:['signs/split-flap','signs/capsule-lightbox','signs/hazard-lightbox'],
    wallLamp:['lights/bulkhead-ring','lights/honeycomb-panel','lights/square-bank','lights/vertical-bar'],
    hangingLamp:['lights/grille-strip','lights/dome-lamp'],
    floorPlant:['plants/snake-plant','plants/rubber-plant','plants/steel-fern','plants/cactus-garden','plants/bamboo'],
    sidePieces:['left-assembly-5','right-assembly-5','left-assembly-6','right-assembly-6'],
    wallSurface:{left:['left/wall-table-fit-v2','left/ivory-wall-cabinet'],right:['right/teal-wall-bench','right/wall-table-fit-v2']},
    wallProp:{left:['left/crt-fit-v2','left/wall-laptop','left/wall-flip-clock','left/wall-reel-recorder'],right:['right/crt-fit-v2','right/wall-radar','right/wall-cassette-deck','right/wall-flip-clock']},
  },
  lab:{
    bench:['left-assembly-3','right-assembly-2','right-assembly-3','left-assembly-1'],
    floorUnit:['utilities/sample-incubator','utilities/coolant-tank','utilities/centrifuge','terminals/lab-instrument','terminals/glass-data-cabinet','terminals/touch-kiosk'],
    wallSign:['signs/arched-nameplate','signs/capsule-lightbox','signs/three-cell-directory'],
    wallLamp:['lights/square-bank','lights/honeycomb-panel','lights/vertical-bar','lights/bulkhead-ring'],
    hangingLamp:['lights/dome-lamp','lights/grille-strip'],
    floorPlant:['plants/monstera','plants/bamboo','plants/steel-fern','plants/olive-palm','plants/rubber-plant','plants/lavender','plants/snake-plant','plants/moss-terrarium'],
    sidePieces:['left-assembly-6','right-assembly-6'],
    wallSurface:{left:['left/ivory-wall-cabinet','left/wall-table-fit-v2'],right:['right/teal-wall-bench','right/burgundy-wall-cabinet']},
    wallProp:{left:['left/wall-laptop','left/wall-task-lamp','left/crt-fit-v2','left/wall-flip-clock'],right:['right/wall-radar','right/wall-inspection-lamp','right/crt-fit-v2','right/wall-flip-clock']},
  },
};
// Ticker housings (ticker-signs-v2), chosen per room from its palette; the visible top of each
// housing in its 512 x 128 canvas, so every sign sits on the beam at the same height.
export const TICKER_TOP={'foundry-led':20,'copper-bulb':4,'ivory-vfd':4,'rail-bulb':5};
const TICKER_FOR={'loading-dock':'foundry-led','copper-foundry':'copper-bulb','relay-workshop':'foundry-led','cooling-bay':'ivory-vfd',
  'turbine-hall':'rail-bulb','ceramic-works':'copper-bulb','orbital-hangar':'foundry-led','subsea-station':'foundry-led','solar-workshop':'copper-bulb',
  'rail-depot':'rail-bulb','signal-room':'rail-bulb','polar-outpost':'ivory-vfd','basalt-forge':'copper-bulb','salvage-workshop':'rail-bulb'};
// Per-room design, decided by looking at each room. Each side wall: a wall-depth surface with one
// piece on it (sidewall-v2) at `feet`, and a free-standing assembly (finishing-v1) further along the
// wall at another depth. Lamps and pendants per room; nothing repeats a neighbour's combination.
// The bulbs stand on each plate's measured trim edge (TRIM_TOP).
const W=(surface,prop,feet,extra,dx=0)=>({surface,prop,feet,extra,dx});
export const DESIGN={
  // ---- industrial workshops
  'loading-dock':{walls:{left:W('left/copper-wall-bench','left/wall-reel-recorder',748,null),right:W('right/burgundy-wall-cabinet','right/wall-cassette-deck',712,null,14)},
    sidePieces:false,frontCorners:false,lightSwitch:[1112,150],lightingScheme:'both',hangingLamp:'lights/grille-strip',wallLamp:'lights/caged-beacon'},
  'copper-foundry':{walls:{left:W('left/wall-table-fit-v2','left/wall-task-lamp',760,'left-assembly-4'),right:W('left/copper-wall-bench','right/wall-flip-clock',700,'right-assembly-6',12)},
    frontCorners:false,extrasOk:true,lightingScheme:'both',hangingLamp:'lights/dome-lamp',wallLamp:'lights/brass-utility',backRow:['bench','unit'],backBenches:['left-assembly-1'],backUnits:['utilities/ventilation-fan']},
  'turbine-hall':{walls:{left:W('right/burgundy-wall-cabinet','left/crt-fit-v2',640,'left-assembly-5'),right:W('right/wall-table-fit-v2','right/wall-radar',665,'right-assembly-1')},
    frontCorners:false,signal:false,lightSwitch:[975,152],lightingScheme:'pillars',wallLamp:'lights/double-task',backRow:['unit','bench'],backUnits:['utilities/piston-compressor'],backBenches:['left-assembly-4']},
  'ceramic-works':{walls:{left:W('left/wall-table-fit-v2','left/wall-flip-clock',629,'left-assembly-2'),right:W('left/ivory-wall-cabinet','right/crt-fit-v2',598,'right-assembly-6')},
    frontCorners:false,signal:[548,178,56],lightSwitch:[985,152],lightingScheme:'both',hangingLamp:'lights/dome-lamp',wallLamp:'lights/retro-sconce'},
  'rail-depot':{walls:{left:W('right/burgundy-wall-cabinet','right/wall-cassette-deck',665,'left-assembly-1'),right:W('left/copper-wall-bench','right/wall-flip-clock',586,'right-assembly-1')},
    frontCorners:false,signal:[545,178,56],lightSwitch:[1000,152],lightingScheme:'both',hangingLamp:'lights/grille-strip',hangingX:[655,877],wallLamp:'lights/bulkhead-ring',backRow:['bench','unit'],backUnits:['utilities/reel-archive']},
  'basalt-forge':{walls:{left:W('left/wall-table-fit-v2','left/wall-reel-recorder',595,'left-assembly-4'),right:W('right/burgundy-wall-cabinet','right/wall-inspection-lamp',653,'right-assembly-1')},
    frontCorners:false,signal:[566,178,52],lightSwitch:[972,152],lightingScheme:'both',hangingLamp:'lights/dome-lamp',wallLamp:'lights/caged-beacon',backRow:['unit','bench'],backUnits:['utilities/ventilation-fan']},
  'salvage-workshop':{walls:{left:W('left/copper-wall-bench','left/wall-laptop',622,'left-assembly-5'),right:W('right/wall-table-fit-v2','right/crt-fit-v2',586,'right-assembly-6')},
    frontCorners:false,signal:[566,178,52],lightSwitch:[972,152],lightingScheme:'pillars',wallLamp:'lights/retro-sconce',backRow:['bench','unit'],backUnits:['utilities/gear-cabinet']},
  // ---- tech workshops
  'relay-workshop':{walls:{left:W('left/wall-table-fit-v2','left/crt-fit-v2',640,null),right:W('right/teal-wall-bench','right/wall-radar',625,null)},
    frontCorners:false,lightingScheme:'none',paintedLights:[[250,34],[407,55],[757,38],[1122,55],[1290,38]],signal:[592,178,56],lightSwitch:[952,150],backRow:['unit','bench'],backUnits:['utilities/coil-regulator']},
  'cooling-bay':{walls:{left:W('left/ivory-wall-cabinet','left/wall-laptop',641,'left-assembly-1'),right:W('right/teal-wall-bench','right/wall-cassette-deck',586,'right-assembly-3')},
    frontCorners:false,lightingScheme:'pillars',wallLamp:'lights/bulkhead-ring',signal:[660,178,52],lightSwitch:[858,150],backRow:['bench','unit'],backUnits:['utilities/air-filter']},
  'orbital-hangar':{walls:{left:W('right/teal-wall-bench','right/wall-radar',640,'left-assembly-6'),right:W('left/ivory-wall-cabinet','left/wall-laptop',647,'right-assembly-2')},
    frontCorners:false,lightingScheme:'both',hangingLamp:'lights/grille-strip',wallLamp:'lights/square-bank',backRow:['unit','bench'],backUnits:['terminals/server-terminal'],backBenches:['right-assembly-3']},
  'subsea-station':{walls:{left:W('left/ivory-wall-cabinet','left/crt-fit-v2',622,'left-assembly-3'),right:W('right/teal-wall-bench','right/wall-flip-clock',640,'right-assembly-3')},
    frontCorners:false,lightingScheme:'pillars',wallLamp:'lights/bulkhead-ring',backRow:['bench','unit'],backUnits:['utilities/coolant-tank']},
  'solar-workshop':{walls:{left:W('left/copper-wall-bench','left/wall-task-lamp',659,'left-assembly-2'),right:W('right/burgundy-wall-cabinet','right/wall-inspection-lamp',580,'right-assembly-6')},
    frontCorners:false,lightingScheme:'both',hangingLamp:'lights/dome-lamp',wallLamp:'lights/vertical-bar',backRow:['unit','bench'],backUnits:['utilities/charging-rack']},
  'signal-room':{walls:{left:W('right/teal-wall-bench','left/wall-reel-recorder',586,'left-assembly-1'),right:W('right/burgundy-wall-cabinet','right/wall-radar',659,'right-assembly-1')},
    frontCorners:false,signal:[564,178,52],lightSwitch:[972,152],lightingScheme:'pillars',wallLamp:'lights/vertical-bar',backRow:['bench','unit'],backUnits:['terminals/round-diagnostic']},
  'polar-outpost':{walls:{left:W('left/ivory-wall-cabinet','left/wall-flip-clock',616,'left-assembly-6'),right:W('right/teal-wall-bench','right/crt-fit-v2',595,'right-assembly-5')},
    frontCorners:false,signal:[564,178,52],lightSwitch:[972,152],lightingScheme:'pillars',wallLamp:'lights/square-bank',backRow:['unit','bench'],backUnits:['utilities/server-drives']},
  // ---- orchestrator labs (the bay holds the result screens; lamps on the pillars)
  'botanical-lab':{walls:{left:W('left/ivory-wall-cabinet','left/wall-task-lamp',629,'left-assembly-3'),right:W('right/teal-wall-bench','right/wall-inspection-lamp',586,'right-assembly-2')},wallLamp:'lights/honeycomb-panel',beside:false},
  'analysis-lab':{walls:{left:W('right/teal-wall-bench','left/wall-laptop',592,'left-assembly-6'),right:W('left/ivory-wall-cabinet','right/wall-radar',653,'right-assembly-3')},wallLamp:'lights/square-bank',beside:false},
  'astronomy-lab':{walls:{left:W('left/ivory-wall-cabinet','left/crt-fit-v2',653,'left-assembly-1'),right:W('right/wall-table-fit-v2','right/wall-radar',580,'right-assembly-6')},wallLamp:'lights/vertical-bar'},
  'crystal-lab':{walls:{left:W('right/teal-wall-bench','left/wall-flip-clock',586,'left-assembly-3'),right:W('left/ivory-wall-cabinet','right/wall-cassette-deck',653,'right-assembly-2')},wallLamp:'lights/bulkhead-ring',beside:false},
  'oceanography-lab':{walls:{left:W('left/wall-table-fit-v2','left/wall-reel-recorder',635,'left-assembly-6'),right:W('right/teal-wall-bench','right/crt-fit-v2',592,'right-assembly-3')},wallLamp:'lights/double-task',beside:false},
  'materials-lab':{walls:{left:W('left/copper-wall-bench','left/wall-laptop',598,'left-assembly-4'),right:W('right/burgundy-wall-cabinet','right/wall-inspection-lamp',659,'right-assembly-2')},wallLamp:'lights/retro-sconce',beside:false},
};
// The front trim's top edge on each plate (measured: the sharpest floor-to-trim brightness drop
// across the bulb strip); bulbs stand just on it.
const TRIM_TOP={'loading-dock':945,'copper-foundry':955,'relay-workshop':955,'cooling-bay':971,'turbine-hall':945,'ceramic-works':944,
  'orbital-hangar':945,'subsea-station':945,'solar-workshop':945,'rail-depot':945,'signal-room':945,'polar-outpost':945,'basalt-forge':944,'salvage-workshop':945};
export const BULBS=['bulbs/caged-beacon','bulbs/dome-beacon','bulbs/ring-pilot','bulbs/porthole','bulbs/ribbed-pilot','bulbs/prismatic-beacon','bulbs/square-indicator'];

// type 'shop' = inference head; 'lab' = orchestrator. Overrides adjust the skeleton per room.
export const ROOMS={
  'loading-dock':{type:'shop',theme:'industrial',title:'Loading Dock'},
  'copper-foundry':{type:'shop',theme:'industrial',title:'Copper Foundry'},
  // Relay Workshop (measured 1:1): door alcoves 205-345 and 1190-1345; pillar columns 492-542 and 988-1035; painted tube lights on the wall.
  'relay-workshop':{type:'shop',theme:'tech',title:'Relay Workshop',overrides:{doors:{b:{center:275,sill:226},a:{center:1267,sill:226}},bay:{x:[620,935],y:[62,150]}}},
  // Louvred hatches stand where the pillars are: hang the lamps and keep the bay between the hatches.
  // Cooling Bay (measured 1:1): louvers 478-605 and 930-1060; pipe boxes 392-465 and 1070-1142; posts 615-640 and 890-918.
  'cooling-bay':{type:'shop',theme:'tech',title:'Cooling Bay',overrides:{bay:{x:[682,842],y:[62,150]},lighting:{pillarCenters:[627,904],pillarFeet:118,pillarHeight:[34,38]}}},
  // Turbine Hall (measured 1:1): turbines in the back corners; door recesses centred 330 and 1202; pillars 465-530 and 1008-1070.
  'turbine-hall':{type:'shop',theme:'industrial',title:'Turbine Hall',overrides:{doors:{b:{center:330,sill:226},a:{center:1202,sill:226}},lighting:{pillarCenters:[497,1039]}}},
  // Ceramic Works (measured 1:1): brick back wall, no alcoves; steel columns 465-530 and 1002-1070 with pipes beside them; kilns in the side walls.
  'ceramic-works':{type:'shop',theme:'industrial',title:'Ceramic Works',overrides:{lighting:{pillarCenters:[497,1036]},bay:{x:[575,976],y:[62,150]}}},
  'orbital-hangar':{type:'shop',theme:'tech',title:'Orbital Hangar'},
  'subsea-station':{type:'shop',theme:'tech',title:'Subsea Station'},
  'solar-workshop':{type:'shop',theme:'tech',title:'Solar Workshop'},
  // Rail Depot (measured 1:1): girders at x 462-522 and 1018-1080; fixture slots on the header beam at 610-700 and 835-920.
  'rail-depot':{type:'shop',theme:'industrial',title:'Rail Depot',overrides:{lighting:{pillarCenters:[492,1049]},bay:{x:[575,976],y:[62,150]}}},
  // Signal Room (measured 1:1): alcoves 198-385 and 1150-1340; pillar columns 462-525 and 1012-1075; pipe boxes 412-458 and 1075-1120.
  'signal-room':{type:'shop',theme:'tech',title:'Signal Room',overrides:{bay:{x:[600,955],y:[62,150]}}},
  // Polar Outpost (measured 1:1): recesses 195-385 and 1150-1340; pillar columns 465-528 and 1010-1070; pipe boxes 415-458 and 1070-1120.
  'polar-outpost':{type:'shop',theme:'tech',title:'Polar Outpost',overrides:{bay:{x:[600,955],y:[62,150]},lighting:{pillarCenters:[497,1040]}}},
  // Basalt Forge (measured 1:1): stone wall, no alcoves; pillar columns 468-525 and 1013-1070; pipe boxes 412-465 and 1070-1125.
  'basalt-forge':{type:'shop',theme:'industrial',title:'Basalt Forge',overrides:{bay:{x:[600,955],y:[62,150]},lighting:{pillarCenters:[497,1041]}}},
  // Salvage Workshop (measured 1:1): recesses 198-385 and 1155-1340; pillar columns 465-530 and 1010-1070; pipe boxes 415-460 and 1070-1120.
  'salvage-workshop':{type:'shop',theme:'industrial',title:'Salvage Workshop',overrides:{bay:{x:[600,955],y:[62,150]},lighting:{pillarCenters:[497,1040]}}},
  // Botanical Lab (measured 1:1): painted doors 190-345 and 1190-1345; dark columns at 366, 510, 1024, 1170; planters built into the wall.
  'botanical-lab':{type:'lab',theme:'lab',title:'Botanical Lab',overrides:{doors:{b:{center:268,sill:226},a:{center:1267,sill:226}},lighting:{pillarCenters:[510,1024]}}},
  // Analysis Lab (measured 1:1): angled end walls 175-320 and 1215-1400; columns at 335, 495, 1032, 1195; recessed panels 370-465 and 1065-1165.
  'analysis-lab':{type:'lab',theme:'lab',title:'Analysis Lab',overrides:{doors:{b:{center:252,sill:226},a:{center:1302,sill:226}},lighting:{pillarCenters:[495,1032]}}},
  'astronomy-lab':{type:'lab',theme:'lab',title:'Astronomy Lab'},
  // Crystal Lab (measured 1:1): recesses 205-385 and 1150-1330; pillar columns 465-530 and 1010-1070; pipe boxes 412-458 and 1070-1120.
  'crystal-lab':{type:'lab',theme:'lab',title:'Crystal Lab',overrides:{lighting:{pillarCenters:[497,1040]}}},
  // Oceanography Lab (measured 1:1): pillar columns 458-530 and 1002-1070; pipe boxes 405-455 and 1075-1130.
  'oceanography-lab':{type:'lab',theme:'lab',title:'Oceanography Lab',overrides:{lighting:{pillarCenters:[494,1036]}}},
  // Materials Lab (measured 1:1): pillar columns 462-530 and 1010-1070; pipe boxes 412-458 and 1070-1120.
  'materials-lab':{type:'lab',theme:'lab',title:'Materials Lab',overrides:{lighting:{pillarCenters:[496,1040]}}},
  // Fixed-layout orchestrator rooms (kit 'plate', 2026-11-11): one application's own room, not in the random pool.
  'ticketing-operations':{type:'lab',kit:'plate',title:'Ticketing Operations',apps:['capstone','capstone-research']},
  'research-laboratory':{type:'lab',kit:'plate',title:'Research Laboratory',apps:['coder']},
};

// The fixed-layout rooms, from the 2026-11-08 art packs (assets/deliveries/2026-11-08-ticketing-research*, copied by
// tools/plate_rooms_assets.py into plate-rooms-v1). Every position was measured on the plate and approved as a
// composite (docs/design/orchestrator-rooms-20261111). Props stand with their ground anchor on `feet` at `scale`;
// `screens` are the dark screen faces in the sprite's own pixels (tools: find the opaque, fully filled dark region),
// `block` the part that agents walk around, `on` a desk it stands on (drawn just in front of it).
const PLATE_DOORS={left:{origin:[146,100],center:213},right:{origin:[1254,102],center:1321}};
// Arena 2.5.0: the desk bay sits BAY_BACK px further back (the operator: move the middle and rear pieces back, more floor).
export const BAY_BACK=60;
const sideDesk=(x)=>({file:'../_shared/side-desk.png',size:[560,390],anchor:[280,390],feet:[x,492-BAY_BACK],scale:.40,block:[20,120,540,390]});
export const PLATES={
  'ticketing-operations':{screenColor:'#6eeb8c',boardColor:'#ffbe5a',boardTitle:'TICKETING OPERATIONS',
    board:{monitor:{file:'props/ticket-wall-monitor.png',size:[640,240],x:528,y:12,scale:.75},text:[569,77,397,62]},
    painted:[[614,40],[768,32],[922,40],[352,80],[1186,80]],
    props:[
      {file:'props/ticket-intake-desk.png',size:[640,400],anchor:[320,380],feet:[768,487-BAY_BACK],scale:.65,screens:[[259,81,380,143]],block:[28,194,611,379]},
      sideDesk(442),{file:'props/ticket-desktop-terminal.png',size:[320,240],anchor:[160,224],feet:[442,388-BAY_BACK],z:493-BAY_BACK,scale:.48,screens:[[107,70,212,138]]},
      sideDesk(1094),{file:'props/ticket-desktop-terminal.png',size:[320,240],anchor:[160,224],feet:[1094,388-BAY_BACK],z:493-BAY_BACK,scale:.48,screens:[[107,70,212,138]]},
      {file:'props/ticket-sorter.png',size:[320,480],anchor:[160,456],feet:[128,760],scale:.55,screens:[[151,194,204,224]],block:[33,63,286,455]},
      {file:'props/ticket-dispatch-cart.png',size:[320,360],anchor:[160,340],feet:[1410,760],scale:.55,block:[40,40,280,340]}]},
  'research-laboratory':{screenColor:'#78e6d2',boardColor:'#78e6d2',boardTitle:'RESEARCH LABORATORY',
    board:{monitor:null,text:[556,58,430,86]},   // the plate's own painted screen is the board (the lab monitor prop is narrower)
    painted:[[610,32],[768,22],[924,32]],
    props:[
      {file:'props/research-bench.png',size:[640,400],anchor:[320,380],feet:[768,487-BAY_BACK],scale:.65,screens:[[400,106,462,152]],block:[54,198,585,379]},
      sideDesk(442),{file:'props/lab-desktop-oscilloscope.png',size:[320,240],anchor:[160,224],feet:[442,388-BAY_BACK],z:493-BAY_BACK,scale:.48,screens:[[36,97,202,191]],wave:true},
      sideDesk(1094),{file:'props/lab-desktop-oscilloscope.png',size:[320,240],anchor:[160,224],feet:[1094,388-BAY_BACK],z:493-BAY_BACK,scale:.48,screens:[[36,97,202,191]],wave:true},
      {file:'props/research-analyzer.png',size:[320,480],anchor:[160,456],feet:[128,760],scale:.55,screens:[[81,267,197,332]],block:[41,57,278,455]},
      {file:'props/lab-specimen-cart.png',size:[320,360],anchor:[160,340],feet:[1410,760],scale:.55,block:[40,40,280,340]}]},
};
// Every file a fixed-layout room loads (tools/room_assets.mjs packs exactly these).
export function plateFiles(id){
  const p=PLATES[id],dir=`plate-rooms-v1/${id}/`,doors=['left','right'].flatMap(s=>[...Array(9).keys()].map(n=>`${dir}doors/${s}/frame-${String(n).padStart(2,'0')}.png`));
  return [`${dir}plate.png`,...doors,...p.props.map(q=>new URL(q.file,'http://x/'+dir).pathname.slice(1)),...(p.board.monitor?[dir+p.board.monitor.file]:[])];
}

export function roomLayout(id){
  const room=ROOMS[id];if(!room)throw new Error('Unknown room '+id);
  // Fixed-layout rooms (another plate architecture: doors at 146-280 and 1254-1388, sill y 258, a back work bay of
  // desks y 240-492, the crowd floor below). Paths: door -> approach (straight down) -> entry (below the desks' row,
  // clear of them and of the corner pieces) -> anywhere on the floor; the staff lane runs in front of the desks.
  if(room.kit==='plate')return {...SKELETON,id,type:'lab',kit:'plate',title:room.title,theme:THEMES.lab,design:{},tickerHousing:null,
    doors:{b:{center:PLATE_DOORS.left.center,sill:258,origin:PLATE_DOORS.left.origin,side:'left'},a:{center:PLATE_DOORS.right.center,sill:258,origin:PLATE_DOORS.right.origin,side:'right'}},
    approachY:330,entries:{b:[262,560-BAY_BACK],a:[1290,560-BAY_BACK]},staffLaneY:575-BAY_BACK,stand:{x:[330,1220],y:[610,880]},work:{x:[440,1100],y:[600,860]},
    dock:[1250,640],lightSwitch:[112,214],plateRoom:PLATES[id],plate:`plate-rooms-v1/${id}/plate.png`,scene:null};
  const o=room.overrides||{};
  // Labs keep the floor under the screens clear: one centred bench or instrument at most.
  const lab=room.type==='lab'?{backFloor:{x:[694,846],feet:[258,264]},backRow:[['bench'],['unit']],lightingScheme:'pillars'}:{};   // labs keep the whole bay for their screens
  return {...SKELETON,...lab,...o,lighting:{...SKELETON.lighting,...(o.lighting||{})},id,type:room.type,title:room.title,theme:THEMES[room.theme],
    tickerHousing:room.type==='lab'?'ivory-vfd':TICKER_FOR[id]||'foundry-led',design:DESIGN[id]||{},
    bulbs:{...SKELETON.bulbs,feet:(TRIM_TOP[id]||932)+6},
    ...(DESIGN[id]?.lightSwitch?{lightSwitch:DESIGN[id].lightSwitch}:{}),
    plate:`composer/${id}/plate.png`,scene:`scenes/${id}/scene.json`};
}
