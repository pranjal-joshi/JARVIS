"use client";

// Android devices over wireless ADB: who's connected, which JARVIS may touch, and QR pairing for
// new phones (Developer options -> Wireless debugging -> Pair device with QR code). Several phones
// can be paired; each gets its own QR.

import { useCallback, useEffect, useState } from "react";
import QRCode from "qrcode";

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });
const STATUS = {
  waiting: "Waiting for the phone to scan…",
  pairing: "Phone found, pairing…",
  connecting: "Paired. Connecting…",
  connected: "Connected.",
  paired: "Paired, but not connected yet: open Wireless debugging on the phone once more.",
  expired: "The code expired. Start again.",
  failed: "Pairing failed.",
};

export default function DevicesPanel({ onCount, version }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState("");
  const [job, setJob] = useState(null); // { id, qr, svg, status, ... }

  const load = useCallback(async () => {
    const r = await api("devices").catch(() => null);
    if (!r?.ok) {
      setError((await r?.json().catch(() => ({})))?.detail || "device bridge unreachable");
      return;
    }
    setError("");
    const d = await r.json();
    setList(d);
    onCount?.(d.filter((x) => x.state === "device").length);
  }, [onCount]);

  useEffect(() => { load(); }, [load, version]);

  // poll the pairing job until it settles
  useEffect(() => {
    if (!job || !["waiting", "pairing", "connecting"].includes(job.status)) return;
    const t = setInterval(async () => {
      const r = await api(`devices/pair/${job.id}`).catch(() => null);
      if (!r?.ok) return;
      const s = await r.json();
      setJob((j) => ({ ...j, ...s }));
      if (s.status === "connected") load();
    }, 1500);
    return () => clearInterval(t);
  }, [job, load]);

  const pair = async () => {
    setJob({ status: "starting" });
    const r = await api("devices/pair", { method: "POST" }).catch(() => null);
    if (!r?.ok) return setJob({ status: "failed", error: (await r?.json().catch(() => ({})))?.detail });
    const j = await r.json();
    const svg = await QRCode.toString(j.qr, { type: "svg", margin: 1, color: { dark: "#7fe7ff", light: "#04070a" } });
    setJob({ ...j, svg });
  };

  const allow = async (serial, allowed) => {
    await api("devices/allow", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ serial, allowed }) });
    load();
    if (job?.serial === serial) setJob((j) => ({ ...j, allowed }));
  };

  const disconnect = async (d) => {
    const r = await api("devices/disconnect", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: d.id }) }).catch(() => null);
    if (!r?.ok) setError((await r?.json().catch(() => ({})))?.detail || "couldn't disconnect");
    load();
  };

  const wireless = (id) => id.includes(":") || id.includes("._adb-tls-connect.");

  return (
    <div className="dev-panel">
      <p className="tools-note">Android devices over wireless ADB. JARVIS only touches devices you allow.</p>
      {error && <p className="dev-err">{error}</p>}
      <ul className="dev-list">
        {(list || []).map((d) => (
          <li key={d.id}>
            <span className={`dev-dot ${d.state === "device" ? "on" : ""}`} aria-hidden="true" />
            <b>{d.model || d.id}</b>
            <i>{d.serial || d.id} · {d.state}</i>
            {d.serial && (
              <button type="button" className={`gate ${d.allowed ? "auto" : "ask"}`} onClick={() => allow(d.serial, !d.allowed)}
                title={d.allowed ? "Click to stop JARVIS using this device" : "Click to let JARVIS use this device"}>
                {d.allowed ? "allowed" : "allow"}
              </button>
            )}
            {wireless(d.id) && (
              <button type="button" className="dev-x" onClick={() => disconnect(d)} aria-label={`Disconnect ${d.model || d.id}`}
                title="Disconnect (reconnect any time from Wireless debugging on the phone)">×</button>
            )}
          </li>
        ))}
        {list && !list.length && <li className="kbx-dim">No devices connected.</li>}
      </ul>
      {!job || ["connected", "expired", "failed", "paired"].includes(job.status) ? (
        <button type="button" className="kbx-btn primary" onClick={pair}>＋ pair a phone (QR)</button>
      ) : null}
      {job && (
        <div className="dev-pair">
          {job.svg && ["waiting", "pairing"].includes(job.status) && (
            <>
              <div className="dev-qr" dangerouslySetInnerHTML={{ __html: job.svg }} />
              <ol>
                <li>Phone and this laptop on the same Wi-Fi.</li>
                <li>Phone: Developer options → <b>Wireless debugging</b> → <b>Pair device with QR code</b>.</li>
                <li>Scan this code.</li>
              </ol>
            </>
          )}
          <p className={`dev-status ${job.status}`}>
            {STATUS[job.status] || "Starting…"}{job.error ? ` ${job.error}` : ""}
            {job.status === "connected" && ` ${job.model || ""} (${job.serial}).`}
          </p>
          {job.status === "connected" && job.serial && !job.allowed && (
            <button type="button" className="kbx-btn primary" onClick={() => allow(job.serial, true)}>allow JARVIS to use it</button>
          )}
        </div>
      )}
    </div>
  );
}
