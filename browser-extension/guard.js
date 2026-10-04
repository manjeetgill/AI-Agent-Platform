// Runs at document_start in the extension's isolated world, before any interaction.
globalThis.__workspaceStopped=false;
for(const event of ['pointerdown','keydown'])document.addEventListener(event,e=>{
  if(e.isTrusted)globalThis.__workspaceStopped=true;
},{capture:true});
