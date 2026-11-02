// Browser audio helpers: record the mic and turn it into the 16 kHz mono PCM
// WAV the hub's Whisper path expects (no ffmpeg needed on the server).

export async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
  });
  const ctx = new AudioContext();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const recorder = new MediaRecorder(stream);
  const chunks = [];
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.start();

  return {
    analyser,
    async stop() {
      const done = new Promise((r) => (recorder.onstop = r));
      recorder.stop();
      await done;
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: recorder.mimeType });
      const wav = await toWav16k(await blob.arrayBuffer(), ctx);
      ctx.close();
      return wav;
    },
  };
}

async function toWav16k(encoded, ctx) {
  const decoded = await ctx.decodeAudioData(encoded);
  const rate = 16000;
  const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
  const src = offline.createBufferSource();
  src.buffer = decoded;
  src.connect(offline.destination);
  src.start();
  const pcm = (await offline.startRendering()).getChannelData(0);

  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const str = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF");
  v.setUint32(4, 36 + pcm.length * 2, true);
  str(8, "WAVEfmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true);
  v.setUint16(22, 1, true);
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, "data");
  v.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

// Play a WAV/MP3 response and expose an analyser so the orb can react to it.
// One reply speaks at a time: starting a new one (or stopAudio) fades out whatever is playing.
let current = null; // { ctx, src, gain, done }

export function stopAudio(fadeMs = 120) {
  const c = current;
  if (!c) return;
  current = null;
  try {
    const t = c.ctx.currentTime;
    c.gain.gain.setValueAtTime(c.gain.gain.value, t);
    c.gain.gain.linearRampToValueAtTime(0, t + fadeMs / 1000);
    c.src.stop(t + fadeMs / 1000);
  } catch {
    c.done();
  }
}

export async function playAudio(blob, onAnalyser) {
  stopAudio();
  const ctx = new AudioContext();
  const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
  const src = ctx.createBufferSource();
  const gain = ctx.createGain();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  src.buffer = buffer;
  src.connect(gain).connect(analyser).connect(ctx.destination);
  let done;
  const ended = new Promise((r) => (done = r));
  src.onended = done;
  stopAudio(); // another reply may have started while this one decoded
  current = { ctx, src, gain, done };
  onAnalyser?.(analyser);
  src.start();
  await ended;
  if (current?.src === src) current = null;
  ctx.close();
}

// Soft two-note chime for "an approval is waiting". Synthesised, so no audio file to ship.
// Browsers only allow sound after the page has had a click or key press; before that it stays silent.
let chimeCtx;
export function chime() {
  try {
    chimeCtx ??= new AudioContext();
    if (chimeCtx.state === "suspended") chimeCtx.resume();
    const t0 = chimeCtx.currentTime;
    [[880, 0], [1318.5, 0.14]].forEach(([freq, at]) => {
      const osc = chimeCtx.createOscillator();
      const gain = chimeCtx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0, t0 + at);
      gain.gain.linearRampToValueAtTime(0.18, t0 + at + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.6);
      osc.connect(gain).connect(chimeCtx.destination);
      osc.start(t0 + at);
      osc.stop(t0 + at + 0.65);
    });
  } catch {
    /* no audio available: the card still shows */
  }
}

// System notification for an approval while the HUD is in the background (another tab, phone
// screen off), so a voice-only user hears "approve it on your phone" and finds it there.
// Needs permission, asked on the first click (askToNotify). Shown through the HUD's service worker
// (public/sw.js) where there is one, since Android Chrome shows notifications no other way; no push
// server is involved, so they only appear while the HUD is open in some tab.
let swReady = null;
export function askToNotify() {
  try {
    if ("serviceWorker" in navigator && window.isSecureContext) swReady ??= navigator.serviceWorker.register("/sw.js")
      .then(() => navigator.serviceWorker.ready).catch(() => null);
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  } catch {}
}
export async function notifyApproval(data) {
  try {
    if (!document.hidden || !("Notification" in window) || Notification.permission !== "granted") return;
    const title = "JARVIS needs your approval";
    const opts = { body: data.says ? `${data.says} (${data.risk})` : `${data.tool} (${data.risk}) asked by ${data.requested_by}`,
      tag: data.approval_id, requireInteraction: true };
    const reg = swReady && (await swReady);
    if (reg) return reg.showNotification(title, opts);
    const n = new Notification(title, opts);
    n.onclick = () => { window.focus(); n.close(); };
  } catch {
    navigator.vibrate?.([200, 100, 200]);
  }
}
