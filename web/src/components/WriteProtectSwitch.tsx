/** 顶栏写保护开关：开启后 agent 不覆盖已存在的文件。只能本机操作。 */
export default function WriteProtectSwitch({
  on,
  onToggle,
  disabled = false,
}: {
  on: boolean | null;
  onToggle: () => void;
  disabled?: boolean;
}) {
  return (
    <label
      className={`wp-switch${disabled ? " off" : ""}`}
      title={
        disabled
          ? "写保护只能在本机（Mac）上改"
          : "写保护：不覆盖已存在的文件"
      }
    >
      <span className="wp-label">写保护</span>
      <span className="switch">
        <input
          type="checkbox"
          checked={!!on}
          disabled={disabled}
          onChange={onToggle}
        />
        <i />
      </span>
    </label>
  );
}
