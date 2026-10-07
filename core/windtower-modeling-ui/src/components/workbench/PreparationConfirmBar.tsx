import { Check, LoaderCircle } from "lucide-react";

interface PreparationConfirmBarProps {
  busy: string;
  engineeringCount: number;
  femCount: number;
  missingCount: number;
  onConfirm: () => void;
  ready: boolean;
  referenceCount: number;
}

export function PreparationConfirmBar({
  busy,
  engineeringCount,
  femCount,
  missingCount,
  onConfirm,
  ready,
  referenceCount,
}: PreparationConfirmBarProps) {
  const disabled = Boolean(busy) || !ready || missingCount > 0;
  return (
    <div className="preparation-confirm-bar" data-testid="preparation-confirm-bar">
      <div className="preparation-counts">
        <span>工程 <strong>{engineeringCount}</strong></span>
        <span>FEM <strong>{femCount}</strong></span>
        <span>参考模型 <strong>{referenceCount}</strong></span>
        <span className={missingCount ? "has-missing" : ""}>缺失 <strong>{missingCount}</strong></span>
      </div>
      <button className="primary-button full" disabled={disabled} onClick={onConfirm} type="button">
        {busy === "confirm-all" ? <LoaderCircle className="spin" size={17} /> : <Check size={17} />}
        {missingCount ? `还需补充 ${missingCount} 项` : "确认全部参数并建立项目"}
      </button>
    </div>
  );
}
