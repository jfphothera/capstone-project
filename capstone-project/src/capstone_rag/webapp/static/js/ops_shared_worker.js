const ports=new Map();let source=null;
const events=['hello','heartbeat','case_arrived','case_updated','simulator','reset'];
function broadcast(value){for(const port of ports.keys())try{port.postMessage(value)}catch{ports.delete(port)}}
function stop(){if(source){source.close();source=null}}
function update(){if(![...ports.values()].some(Boolean)){stop();return}if(source)return;source=new EventSource('/api/ops/events');for(const name of events)source.addEventListener(name,event=>broadcast({event:name,data:event.data}));source.onerror=()=>broadcast({event:'connection_error'})}
onconnect=event=>{const port=event.ports[0];ports.set(port,true);port.onmessage=message=>{const value=message.data||{};if(value.type==='visibility')ports.set(port,Boolean(value.visible));if(value.type==='disconnect')ports.delete(port);update()};port.start();update()};
