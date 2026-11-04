/** 顶栏写保护开关：开启后 agent 不覆盖已存在的文件。 */
export default function WriteProtectSwitch({
  on,
  onToggle,
}: {
  on: boolean | null;
  onToggle: () => void;
}) {
  return (
    <label className="wp-switch" title="写保护：不覆盖已存在的文件">
      <span className="wp-label">写保护</span>
      <span className="switch">
        <input type="checkbox" checked={!!on} onChange={onToggle} />
        <i />
      </span>
    </label>
  );
}
