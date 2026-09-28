import { useState } from "react";
import { Icon } from "./Icons";
import { fmtNumber } from "../util";

const ACU_TO_USD = 2;

interface ConfigModalProps {
  isOpen: boolean;
  onClose: () => void;
  config: Record<string, string>;
  onSave: (updates: Record<string, string>) => Promise<void>;
}

const CONFIG_LABELS: Record<string, string> = {
  MAX_ACTIVE_SESSIONS: "Max Active Sessions",
  REPAIR_ACU_LIMIT: "Repair ACU Limit",
  DAILY_ADMISSION_ACU_LIMIT: "Daily Admission ACU Limit",
  PROJECT_ADMISSION_ACU_LIMIT: "Project Admission ACU Limit",
  POLL_INTERVAL_SECONDS: "Poll Interval (seconds)",
  SCAN_INTERVAL_SECONDS: "Scan Interval (seconds)",
  REPORT_PUBLISH_INTERVAL_SECONDS: "Report Publish Interval (seconds)",
  REPORT_STALE_AFTER_SECONDS: "Report Stale After (seconds)",
};

const CONFIG_DESCRIPTIONS: Record<string, string> = {
  MAX_ACTIVE_SESSIONS: "Maximum number of concurrent Devin sessions",
  REPAIR_ACU_LIMIT: "ACU budget per repair session",
  DAILY_ADMISSION_ACU_LIMIT: "Total ACU budget per day",
  PROJECT_ADMISSION_ACU_LIMIT: "Total ACU budget for the project",
  POLL_INTERVAL_SECONDS: "How often to poll session status",
  SCAN_INTERVAL_SECONDS: "How often to scan GitHub for new issues",
  REPORT_PUBLISH_INTERVAL_SECONDS: "How often to publish reports",
  REPORT_STALE_AFTER_SECONDS: "After how long reports are considered stale",
};

export default function ConfigModal({
  isOpen,
  onClose,
  config,
  onSave,
}: ConfigModalProps) {
  const [updates, setUpdates] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!isOpen) return null;

  const handleChange = (key: string, value: string) => {
    setUpdates((prev) => ({ ...prev, [key]: value }));
    setError(null);
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await onSave(updates);
      setUpdates({});
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save configuration");
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setUpdates({});
    setError(null);
  };

  const currentValue = (key: string) => updates[key] ?? config[key] ?? "";

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Configuration</h2>
          <button className="icon-button" onClick={onClose} aria-label="Close">
            <Icon.Close size={20} />
          </button>
        </div>

        <div className="modal-body">
          <p className="small text-muted">
            Changes saved here override environment defaults and persist in the
            database.
          </p>

          {error && (
            <div className="alert alert-error" role="alert">
              {error}
            </div>
          )}

          <div className="config-form">
            {Object.entries(CONFIG_LABELS).map(([key, label]) => (
              <div key={key} className="form-group">
                <label htmlFor={key}>
                  {label}
                  <span className="help-text">
                    {CONFIG_DESCRIPTIONS[key]}
                  </span>
                </label>
                <input
                  id={key}
                  type="number"
                  min="0"
                  step={key.includes("INTERVAL") ? "1" : "1"}
                  value={currentValue(key)}
                  onChange={(e) => handleChange(key, e.target.value)}
                  className={updates[key] ? "input-changed" : ""}
                />
                {key.includes("ACU") && (
                  <span className="acu-usd">
                    ≈ ${fmtNumber(Number(currentValue(key)) * ACU_TO_USD)} USD
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="modal-footer">
          <button
            className="button button-secondary"
            onClick={handleReset}
            disabled={saving || Object.keys(updates).length === 0}
          >
            Reset
          </button>
          <button
            className="button button-primary"
            onClick={handleSave}
            disabled={saving || Object.keys(updates).length === 0}
          >
            {saving ? "Saving..." : "Save Changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
