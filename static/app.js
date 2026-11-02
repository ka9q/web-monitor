'use strict';
const $ = id => document.getElementById(id);
let saved = {};
try { saved = JSON.parse(localStorage.getItem('web-monitor-v1') || '{}'); } catch (_) {}
if (!saved || typeof saved !== 'object') saved = {};
const selected = new Set(Array.isArray(saved.selected) ? saved.selected.filter(x => typeof x === 'string') : []);
const volumes = saved.volumes && typeof saved.volumes === 'object' ? saved.volumes : {};
const pans = saved.pans && typeof saved.pans === 'object' ? saved.pans : {};
let catalogue = [], iceServers = [], maxChannels = 32;
let context, master, active, connecting = false, enabled = false, revision = 0, retry;
const rows = new Map();
const sortKeys = ['name', 'frequency', 'group', 'activity', 'ssrc'];
let sortKey = sortKeys.includes(saved.sortKey) ? saved.sortKey : 'name';
let descending = saved.descending === true;
let selectedFirst = saved.selectedFirst === true;
const collator = new Intl.Collator(undefined, {numeric:true, sensitivity:'base'});
function frequencyHz(value) {
  if(typeof value === 'number') return Number.isFinite(value) && value >= 0 ? value : null;
  const match = String(value ?? '').trim().match(/^(\d+(?:\.\d+)?)\s*(GHz|MHz|kHz|Hz)?$/i);
  if(!match) return null;
  return Number(match[1]) * ({ghz:1e9, mhz:1e6, khz:1e3, hz:1}[(match[2] || 'Hz').toLowerCase()]);
}
function channelTitle(channel) {
  if(!channel.frequency) return channel.name;
  const suffix = ` · SSRC ${channel.ssrc}`;
  const name = channel.name.endsWith(suffix) ? channel.name.slice(0, -suffix.length) : channel.name;
  return `${name} · ${channel.frequency}`;
}
function sortedChannels() {
  const text = (a, b) => collator.compare(String(a ?? ''), String(b ?? ''));
  return [...catalogue].sort((a, b) => {
    if(selectedFirst && selected.has(a.id) !== selected.has(b.id)) return selected.has(a.id) ? -1 : 1;
    let result = 0;
    if(sortKey === 'frequency') {
      const left = frequencyHz(a.frequency_hz ?? a.frequency), right = frequencyHz(b.frequency_hz ?? b.frequency);
      if((left === null) !== (right === null)) return left === null ? 1 : -1;
      result = left === null ? 0 : left - right;
    } else if(sortKey === 'activity') result = Number(!a.active) - Number(!b.active);
    else if(sortKey === 'ssrc') result = a.ssrc - b.ssrc;
    else result = sortKey === 'name' ? text(channelTitle(a), channelTitle(b)) : text(a[sortKey], b[sortKey]);
    // Stable tie-breaks prevent rows jumping as the catalogue is refreshed.
    return (descending ? -result : result) || text(channelTitle(a), channelTitle(b)) || text(a.group, b.group) || text(a.id, b.id);
  });
}
function updateSortControls() {
  $('sort').value = sortKey;
  $('sort-direction').textContent = descending ? 'Descending ↓' : 'Ascending ↑';
  $('selected-first').checked = selectedFirst;
}
function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }
function save() { try { localStorage.setItem('web-monitor-v1', JSON.stringify({selected:[...selected], volumes, pans, sortKey, descending, selectedFirst})); } catch (_) {} }
function gainFor(id) { const v = Number(volumes[id] ?? .8); return Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : .8; }
function panFor(id) { const p = Number(pans[id] ?? 0); return Number.isFinite(p) ? Math.max(-1, Math.min(1, p)) : 0; }
function panLabel(value) { return Math.abs(value) < .005 ? 'Center' : `${Math.round(Math.abs(value) * 100)}% ${value < 0 ? 'left' : 'right'}`; }
function setPan(id) { const graph = active?.graphs.get(id); if(graph) graph.panner.pan.setTargetAtTime(panFor(id), context.currentTime, .015); }
function updateMaster() { if(master) master.gain.value = $('mute').checked ? 0 : Number($('master').value); }
function chosen(ids = catalogue.map(c => c.id)) { return ids.filter(id => selected.has(id)); }
async function api(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers:body === undefined ? {} : {'Content-Type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body), cache:'no-store'});
  if (!response.ok) throw new Error((await response.text()).slice(0, 240) || `HTTP ${response.status}`);
  return response.status === 204 ? null : response.json();
}
async function release(session) {
  if(!session) return;
  session.pc.close();
  for(const graph of session.graphs.values()) { graph.source.disconnect(); graph.gain.disconnect(); graph.panner.disconnect(); }
  if(session.id) { try { await api(`/api/sessions/${session.id}`, 'DELETE'); } catch (_) {} }
}
function setVolume(id) { const graph = active?.graphs.get(id); if(graph) graph.gain.gain.value = gainFor(id); }
function render() {
  const present = new Set(catalogue.map(c => c.id));
  for(const [id, row] of rows) if(!present.has(id)) { row.remove(); rows.delete(id); }
  for(const channel of catalogue) {
    let row = rows.get(channel.id);
    if(!row) {
      row = $('channel-template').content.firstElementChild.cloneNode(true);
      rows.set(channel.id, row); $('channels').append(row);
      const checkbox = row.querySelector('[type=checkbox]');
      checkbox.addEventListener('change', () => {
        checkbox.checked ? selected.add(channel.id) : selected.delete(channel.id);
        save(); render();
        if(checkbox.checked) startAudio(false); else syncSelection();
      });
      const volume = row.querySelector('.volume input');
      volume.setAttribute('aria-label', `Volume for ${channel.name}`);
      volume.value = gainFor(channel.id);
      volume.addEventListener('input', () => { volumes[channel.id] = Number(volume.value); setVolume(channel.id); save(); });
      const pan = row.querySelector('.pan input');
      const readout = row.querySelector('.pan output');
      pan.setAttribute('aria-label', `Pan for ${channel.name}`);
      pan.value = panFor(channel.id);
      function showPan() { const label = panLabel(Number(pan.value)); readout.textContent = label; pan.setAttribute('aria-valuetext', label); }
      showPan();
      pan.addEventListener('input', () => { pans[channel.id] = Number(pan.value); showPan(); setPan(channel.id); save(); });
      pan.addEventListener('dblclick', () => { pan.value = 0; pans[channel.id] = 0; showPan(); setPan(channel.id); save(); });
    }
    const title = channelTitle(channel);
    row.querySelector('.name').textContent = title;
    row.querySelector('.volume input').setAttribute('aria-label', `Volume for ${title}`);
    row.querySelector('.pan input').setAttribute('aria-label', `Pan for ${title}`);
    const source = channel.source_name ? `${channel.source_name} (${channel.source})` : channel.source;
    row.querySelector('.details').textContent = [channel.mode, channel.group, `${source} / ${channel.ssrc}`].filter(Boolean).join(' · ');
    const checkbox = row.querySelector('[type=checkbox]');
    checkbox.checked = selected.has(channel.id);
    row.classList.toggle('selected', checkbox.checked);
    const activity = row.querySelector('.activity');
    activity.textContent = channel.active ? 'Receiving' : 'Quiet';
    activity.classList.toggle('live', channel.active);
  }
  const focused = document.activeElement;
  sortedChannels().forEach((channel, index) => {
    const row = rows.get(channel.id);
    if($('channels').children[index] !== row) $('channels').insertBefore(row, $('channels').children[index] || null);
  });
  if(focused && document.contains(focused) && document.activeElement !== focused) focused.focus({preventScroll:true});
  $('connect').hidden = !enabled && chosen().length === 0;
  $('connect').textContent = enabled ? 'Reconnect audio' : 'Start selected audio';
  $('stop').disabled = !enabled;
  $('count').textContent = catalogue.length;
  $('empty').hidden = catalogue.length > 0;
  $('summary').textContent = `${chosen().length} selected · ${active ? 'WebRTC ' + active.pc.connectionState : 'not connected'}`;
}
async function syncSelection() {
  if(!enabled || connecting) return;
  if(!active || chosen().some(id => !active.ids.includes(id))) return connect();
  try { await api(`/api/sessions/${active.id}`, 'PUT', {selected:chosen(active.ids)}); }
  catch(error) { status(error.message, true); scheduleReconnect(); }
}
function scheduleReconnect() {
  clearTimeout(retry);
  if(enabled) retry = setTimeout(() => connect(), 3000);
}
function gathered(pc) {
  if(pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pc.removeEventListener('icegatheringstatechange', check); reject(new Error('ICE gathering timed out')); }, 20000);
    function check() { if(pc.iceGatheringState === 'complete') { clearTimeout(timer); pc.removeEventListener('icegatheringstatechange', check); resolve(); } }
    pc.addEventListener('icegatheringstatechange', check); check();
  });
}
async function connect() {
  if(!enabled || connecting) return;
  if(!catalogue.length) { status('Waiting for streams. Audio will connect when a channel is discovered.'); return; }
  if(chosen().length > maxChannels) { status(`Select at most ${maxChannels} channels at a time.`, true); return; }
  connecting = true;
  const generation = revision;
  const ids = [...new Set([...chosen(), ...catalogue.map(c => c.id)])].slice(0, maxChannels);
  const session = {pc:new RTCPeerConnection({iceServers, bundlePolicy:'max-bundle'}), ids, id:null, graphs:new Map()};
  const pc = session.pc;
  const transceivers = ids.map(() => pc.addTransceiver('audio', {direction:'recvonly'}));
  status('Connecting audio…');
  pc.ontrack = event => {
    const index = transceivers.indexOf(event.transceiver);
    if(index < 0) return;
    const id = ids[index];
    const source = context.createMediaStreamSource(new MediaStream([event.track]));
    const gain = context.createGain(); gain.gain.value = gainFor(id);
    const panner = context.createStereoPanner(); panner.pan.value = panFor(id);
    source.connect(gain).connect(panner).connect(master);
    session.graphs.set(id, {source, gain, panner});
  };
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await gathered(pc);
    const answer = await api('/api/sessions', 'POST', {type:'offer', sdp:pc.localDescription.sdp, channels:ids, selected:[]});
    session.id = answer.id;
    if(!enabled || revision !== generation) { await release(session); return; }
    await pc.setRemoteDescription({type:answer.type, sdp:answer.sdp});
    const old = active;
    active = session;
    pc.onconnectionstatechange = () => {
      if(active !== session || !enabled) return;
      render();
      if(pc.connectionState === 'connected') status('Listening. Uncheck a channel to stop its media traffic.');
      if(['failed','disconnected'].includes(pc.connectionState)) { status('Audio connection interrupted; retrying…', true); scheduleReconnect(); }
    };
    await release(old);
    await api(`/api/sessions/${session.id}`, 'PUT', {selected:chosen(ids)});
    status('Audio enabled. Waiting for the WebRTC connection…');
  } catch(error) {
    if(active === session) active = null;
    await release(session);
    if(enabled) { status(error.message, true); scheduleReconnect(); }
  } finally {
    connecting = false; render();
    if(enabled && active && chosen().some(id => !active.ids.includes(id))) scheduleReconnect();
  }
}
async function startAudio(reconnect = false) {
  try {
    if(!window.RTCPeerConnection || !window.AudioContext) throw new Error('This browser needs WebRTC and Web Audio support. Use HTTPS or localhost.');
    // Called directly by a click/change handler to satisfy browser autoplay rules.
    if(!context) { context = new AudioContext(); master = context.createGain(); master.connect(context.destination); updateMaster(); }
    await context.resume();
    enabled = true; render();
    clearTimeout(retry);
    if(reconnect || !active) await connect(); else await syncSelection();
  } catch(error) { status(error.message, true); }
}
$('connect').addEventListener('click', () => startAudio(true));
$('stop').addEventListener('click', async () => {
  enabled = false; revision++; clearTimeout(retry);
  const old = active; active = null; await release(old);
  $('stop').disabled = true; status('Disconnected. Selections are saved.'); render();
});
$('sort').addEventListener('change', () => { sortKey = $('sort').value; save(); render(); });
$('sort-direction').addEventListener('click', () => { descending = !descending; updateSortControls(); save(); render(); });
$('selected-first').addEventListener('change', () => { selectedFirst = $('selected-first').checked; save(); render(); });
updateSortControls();
$('master').addEventListener('input', updateMaster);
$('mute').addEventListener('change', updateMaster);
async function refresh() {
  try {
    const data = await api('/api/channels');
    const previousIds = catalogue.map(c => c.id).join(',');
    catalogue = data.channels; iceServers = data.ice_servers; maxChannels = data.max_channels;
    $('title').textContent = data.title; document.title = data.title;
    render();
    if(enabled && !active && !connecting) connect();
    else if(enabled && active && !connecting && previousIds !== catalogue.map(c => c.id).join(',')) syncSelection();
    else if(!enabled) status(catalogue.length ? (chosen().length ? 'Saved selections restored. Click Start selected audio, or select another channel, to listen.' : 'Select a channel to start listening.') : 'Waiting for Opus streams in the configured groups.');
  } catch(error) { status(`Gateway unavailable: ${error.message}`, true); }
}
setInterval(refresh, 5000);
setInterval(() => { if(enabled && active && !connecting) syncSelection(); }, 15000);
window.addEventListener('pagehide', () => { if(active) { active.pc.close(); fetch(`/api/sessions/${active.id}`, {method:'DELETE', keepalive:true}).catch(() => {}); } });
refresh();
