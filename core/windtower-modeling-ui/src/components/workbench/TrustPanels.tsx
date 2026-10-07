import { AlertTriangle, ArrowLeft, Check, Circle, History, X } from "lucide-react";

import type { ParameterTrace, RevisionDiff, RevisionList, ValidationSummary } from "../../types";

export interface ReviewTableItem {
  key: string;
  label: string;
  value: unknown;
  unit?: string | null;
  source: string;
  status: "confirmed" | "candidate" | "review" | "derived";
  additionalSources?: number;
}

export function ParameterReviewTable({ items, onSelect }: { items: ReviewTableItem[]; onSelect: (item: ReviewTableItem) => void }) {
  return <div className="parameter-review-table" role="table" aria-label="参数证据审查">
    <div className="parameter-review-head" role="row"><span>Parameter</span><span>Value</span><span>Source</span><span>Status</span></div>
    {[...items].sort((a, b) => rank(a.status) - rank(b.status)).map((item) => <button className="parameter-review-row" key={item.key} onClick={() => onSelect(item)} role="row" type="button">
      <strong>{item.label}</strong><span>{formatValue(item.value)} {item.unit ?? ""}</span><span title={item.source}>{item.source}{item.additionalSources ? ` +${item.additionalSources}` : ""}</span><ReviewStatus value={item.status} />
    </button>)}
  </div>;
}

export function ParameterTracePanel({ trace, loading, onBack }: { trace: ParameterTrace | null; loading: boolean; onBack: () => void }) {
  if (loading) return <div className="panel-loading">正在读取证据链…</div>;
  return <div className="trace-panel">
    <button className="inline-back" onClick={onBack} type="button"><ArrowLeft size={14} />返回参数列表</button>
    {!trace ? <p>未找到参数追溯信息。</p> : <>
      <div className="trace-heading"><div><small>Evidence → Parameter → FE entity → Artifact</small><strong>{trace.label}</strong></div><ReviewStatus value={trace.confirmation_status === "confirmed" ? "confirmed" : "review"} /></div>
      <dl className="trace-facts">
        <div><dt>Extracted value</dt><dd>{formatValue(trace.raw_value)} {trace.raw_unit ?? ""}</dd></div>
        <div><dt>Normalized value</dt><dd>{formatValue(trace.normalized_value)} {trace.normalized_unit ?? ""}</dd></div>
        <div><dt>Mapping confidence</dt><dd>{formatConfidence(trace.mapping_confidence ?? trace.confidence)}</dd></div>
        <div><dt>Source category</dt><dd>{trace.assurance?.source_category ?? "—"}</dd></div>
        <div><dt>Locator</dt><dd>{trace.assurance?.locator_status ?? "unverified"}</dd></div>
        <div><dt>Formal use</dt><dd>{trace.assurance?.formal_use_allowed ? "allowed" : "blocked"}</dd></div>
      </dl>
      <section className="trace-section"><strong>Sources</strong>{trace.sources.length ? trace.sources.map((source, index) => <details key={`${source.filename}-${index}`} open={index === 0}><summary>{source.filename ?? "未命名来源"}<small>{source.locator ?? "位置未记录"}</small></summary>{source.excerpt ? <p>{source.excerpt}</p> : null}</details>) : <p>没有可定位来源，正式使用应被阻断。</p>}</section>
      <section className="trace-section"><strong>Consumed by</strong><div className="consumer-list">{trace.consumed_by.map((item) => <span key={item}>{item}</span>)}</div></section>
    </>}
  </div>;
}

export function ValidationPanel({ summary }: { summary: ValidationSummary | null }) {
  if (!summary) return <div className="compact-empty"><Circle size={24} /><strong>Validation 尚未运行</strong><span>参数、模型/网格、求解/结果将分别显示。</span></div>;
  return <div className="validation-panel">
    <div className="validation-overview"><span>Parameter Revision</span><strong>R{summary.parameter_revision}</strong><StatusIcon status={summary.status} /></div>
    <div className="validation-groups">{summary.groups.map((group) => <details key={group.id} open={group.status !== "pass"}>
      <summary><StatusIcon status={group.status} /><strong>{group.label}</strong><small>{group.counts.passed} pass · {group.counts.warnings} warning · {group.counts.failed} fail · {group.counts.not_run} not run</small></summary>
      {group.checks.map((check) => <div className="validation-check" key={check.id}><StatusIcon status={check.status} /><span>{check.label}</span><em>{check.value == null ? check.status.replace("_", " ") : formatValue(check.value)}</em></div>)}
    </details>)}</div>
  </div>;
}

export function RevisionPanel({ revisions, diff, onBack }: { revisions: RevisionList | null; diff: RevisionDiff | null; onBack: () => void }) {
  return <div className="revision-panel"><button className="inline-back" onClick={onBack} type="button"><ArrowLeft size={14} />返回参数审查</button><div className="revision-heading"><strong>Revision history</strong><History size={18} /></div>
    {(revisions?.items ?? []).map((item) => <div className="revision-row" key={item.revision}><span>R{item.revision}</span><strong>{item.summary}</strong><small>{item.operator}</small></div>)}
    {(diff?.diff ?? []).map((item) => <div className="revision-change" key={item.key}><strong>{item.label}</strong><del>{formatValue(item.old_value)}</del><span>→</span><ins>{formatValue(item.new_value)} {item.unit ?? ""}</ins></div>)}
  </div>;
}

function ReviewStatus({ value }: { value: ReviewTableItem["status"] }) {
  return <span className={`review-status ${value}`}>{value === "confirmed" ? <Check size={11} /> : value === "review" ? <AlertTriangle size={11} /> : <Circle size={10} />}{value}</span>;
}

function StatusIcon({ status }: { status: "pass" | "warning" | "fail" | "not_run" }) {
  return <span className={`validation-icon ${status}`}>{status === "pass" ? <Check size={11} /> : status === "warning" ? <AlertTriangle size={11} /> : status === "fail" ? <X size={11} /> : <Circle size={10} />}</span>;
}

function rank(value: ReviewTableItem["status"]) { return { review: 0, candidate: 1, confirmed: 2, derived: 3 }[value]; }
function formatConfidence(value?: number | null) { return typeof value === "number" ? value.toFixed(2) : "—"; }
function formatValue(value: unknown) { return value == null || value === "" ? "—" : typeof value === "object" ? JSON.stringify(value) : String(value); }
