"use client";

// Guard: what JARVIS may do without asking (policy/policy.yaml, read-only here), the grants given on
// approval cards ("allow for 1 hour", revocable), and how the eval suite scored each model over time.

import { useCallback, useEffect, useState } from "react";

const api = (path, init) => fetch(`/api/hub/${path}`, { cache: "no-store", ...init });
const until = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export default function GuardPanel({ version }) {
  const [policy, setPolicy] = useState(null);
  const [evals, setEvals] = useState([]);

  const load = useCallback(async () => {
    const [p, e] = await Promise.all([api("policy").catch(() => null), api("evals").catch(() => null)]);
    if (p?.ok) setPolicy(await p.json());
    if (e?.ok) setEvals((await e.json()).slice().reverse());
  }, []);

  useEffect(() => { load(); }, [load, version]);

  const revoke = async (tool) => {
    await api(`policy/grants/${encodeURIComponent(tool)}`, { method: "DELETE" });
    load();
  };

  return (
    <div className="dev-panel guard-panel">
      <h4>Policy</h4>
      {policy ? (
        <>
          <p className="tools-note">
            {policy.file ? <>From <b>policy/policy.yaml</b> (edit it by hand; changes apply at once).</>
              : <>No policy file: APPROVAL_RISKS decides.</>} A write carrying text from a web page, message or
            file always asks, whatever the policy says.
          </p>
          <ul className="guard-defaults">
            {Object.entries(policy.defaults).map(([risk, action]) => (
              <li key={risk}><span className={`gate ${action === "allow" ? "auto" : "ask"}`}>{action}</span> {risk}</li>
            ))}
          </ul>
          {Object.values(policy.budgets).some(Boolean) && (
            <p className="kbx-dim">
              Budgets: {policy.budgets.external_per_day ? `${policy.budgets.external_per_day} outside calls a day` : ""}
              {policy.budgets.external_per_day && policy.budgets.writes_per_hour ? " · " : ""}
              {policy.budgets.writes_per_hour ? `${policy.budgets.writes_per_hour} changes an hour` : ""}
            </p>
          )}
          {policy.rules.length > 0 && (
            <ul className="guard-rules">
              {policy.rules.map((r, i) => (
                <li key={i}>
                  <span className={`gate ${r.action === "allow" ? "auto" : "ask"}`}>{r.action}</span>
                  <b>{r.tool}</b>
                  <i>{[r.source?.length ? `from ${r.source.join(", ")}` : "", r.hours ? `${r.hours} h` : "",
                    Object.keys(r.args || {}).length ? "some arguments" : ""].filter(Boolean).join(" · ")}</i>
                  {r.why && <small>{r.why}</small>}
                </li>
              ))}
            </ul>
          )}
          <h4>Allowed for a while</h4>
          <ul className="guard-rules">
            {policy.grants.map((g) => (
              <li key={g.tool + g.until}>
                <b>{g.tool}</b><i>from {g.source} until {until(g.until)}</i>
                <button type="button" className="gate ask" onClick={() => revoke(g.tool)}>revoke</button>
              </li>
            ))}
            {!policy.grants.length && <li className="kbx-dim">None. "Allow 1 h" on an approval card adds one.</li>}
          </ul>
        </>
      ) : <p className="kbx-dim">Loading…</p>}

      <h4>Evals</h4>
      <p className="tools-note">Run <b>python scripts/eval/run_eval.py</b>: fixed prompts in dry run (nothing is saved or
        sent), scored on tool choice, arguments, style and speed.</p>
      <ul className="guard-evals">
        {evals.map((r, i) => (
          <li key={i}>
            <div className="skill-head">
              <b>{r.model}</b>
              <span className={`gate ${r.passed === r.cases ? "auto" : "ask"}`}>{r.passed}/{r.cases}</span>
              <i>{r.when?.replace("T", " ")} · first words {r.first_words_median_s ?? "?"} s</i>
            </div>
            <small>{Object.entries(r.by_category || {}).map(([c, v]) => `${c} ${v}`).join(" · ")}</small>
            {r.failed?.length > 0 && (
              <details>
                <summary>{r.failed.length} failed</summary>
                {r.failed.map((f) => <div key={f.id}><b>{f.id}</b> {f.fails.join("; ")}</div>)}
              </details>
            )}
          </li>
        ))}
        {!evals.length && <li className="kbx-dim">No runs yet.</li>}
      </ul>
    </div>
  );
}
