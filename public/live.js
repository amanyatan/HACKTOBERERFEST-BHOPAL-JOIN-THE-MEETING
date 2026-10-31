// Minimal Gemini Live client: ephemeral token -> WebSocket, mic PCM16@16k up, PCM16@24k down.
const WS_URL = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained';

const b64 = (buf) => { let s = ''; const u = new Uint8Array(buf); for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode(...u.subarray(i, i + 0x8000)); return btoa(s); };

const WORKLET = `class P extends AudioWorkletProcessor {
  process(inputs) { const c = inputs[0][0]; if (c) { const o = new Int16Array(c.length);
    for (let i = 0; i < c.length; i++) o[i] = Math.max(-1, Math.min(1, c[i])) * 32767; this.port.postMessage(o.buffer, [o.buffer]); } return true; } }
registerProcessor('p', P);`;

// h: {onSpeaking(bool), onUserText(t), onAgentText(t), onToolCall(name,args), onFarewellDone(), onClose()}
export async function startLive(agent, micStream, h, context = '', kickoff = 'The user just joined the meeting. Greet them.') {
  const r = await fetch('/api/token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent, context }) });
  if (!r.ok) throw new Error((await r.json()).detail || 'token failed');
  const { token, model } = await r.json();

  const ws = new WebSocket(`${WS_URL}?access_token=${encodeURIComponent(token)}`);
  const inCtx = new AudioContext({ sampleRate: 16000 });
  const outCtx = new AudioContext({ sampleRate: 24000 });
  let playAt = 0, sources = [], closing = false, toolSent = false, farewell = false;

  const stopPlayback = () => { sources.forEach((s) => { try { s.stop(); } catch {} }); sources = []; playAt = 0; h.onSpeaking(false); };
  const play = (data) => {
    const pcm = new Int16Array(Uint8Array.from(atob(data), (c) => c.charCodeAt(0)).buffer);
    const buf = outCtx.createBuffer(1, pcm.length, 24000);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const src = outCtx.createBufferSource(); src.buffer = buf; src.connect(outCtx.destination);
    playAt = Math.max(playAt, outCtx.currentTime + 0.05); src.start(playAt); playAt += buf.duration;
    sources.push(src); h.onSpeaking(true);
    src.onended = () => { sources = sources.filter((s) => s !== src); if (!sources.length) h.onSpeaking(false); };
  };

  const stop = () => { closing = true; stopPlayback(); try { ws.close(); } catch {} inCtx.close(); outCtx.close(); };

  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('Live connection failed')); });
  ws.send(JSON.stringify({ setup: { model: `models/${model}` } }));

  ws.onmessage = async (e) => {
    const m = JSON.parse(typeof e.data === 'string' ? e.data : await e.data.text());
    if (m.setupComplete) {
      await inCtx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' })));
      const node = new AudioWorkletNode(inCtx, 'p');
      node.port.onmessage = (ev) => ws.readyState === 1 && ws.send(JSON.stringify({ realtimeInput: { audio: { data: b64(ev.data), mimeType: 'audio/pcm;rate=16000' } } }));
      inCtx.createMediaStreamSource(micStream).connect(node);
      ws.send(JSON.stringify({ realtimeInput: { text: kickoff } }));
    }
    const sc = m.serverContent;
    if (sc) {
      for (const p of sc.modelTurn?.parts || []) if (p.inlineData?.data) { play(p.inlineData.data); if (toolSent) farewell = true; }
      if (sc.turnComplete && toolSent && farewell) { toolSent = farewell = false; h.onFarewellDone(); }
      if (sc.interrupted) stopPlayback();
      if (sc.inputTranscription?.text) h.onUserText(sc.inputTranscription.text);
      if (sc.outputTranscription?.text) h.onAgentText(sc.outputTranscription.text);
    }
    for (const fc of m.toolCall?.functionCalls || []) {
      h.onToolCall(fc.name, fc.args);
      ws.send(JSON.stringify({ toolResponse: { functionResponses: [{ id: fc.id, name: fc.name, response: { ok: true } }] } }));
      toolSent = true;
      setTimeout(() => toolSent && h.onFarewellDone(), 15000); // fallback if no farewell turn arrives
    }
  };
  ws.onclose = () => { if (!closing) h.onClose(); };
  outCtx.resume(); inCtx.resume();
  const sendText = (text) => ws.readyState === 1 && ws.send(JSON.stringify({ realtimeInput: { text } }));
  return { stop, sendText };
}
