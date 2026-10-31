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
export async function playAudio(blob, onAnalyser) {
  const ctx = new AudioContext();
  const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
  const src = ctx.createBufferSource();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  src.buffer = buffer;
  src.connect(analyser).connect(ctx.destination);
  onAnalyser?.(analyser);
  const ended = new Promise((r) => (src.onended = r));
  src.start();
  await ended;
  ctx.close();
}
