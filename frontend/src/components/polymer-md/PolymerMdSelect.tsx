import { useId, type ReactNode } from "react";
import { WorkbenchSelect, type WorkbenchSelectOption } from "../structure-workbench/WorkbenchSelect";

export function PolymerMdSelect<T extends string>({
  id,
  label,
  value,
  options,
  onChange,
  describedBy,
  invalid,
  children
}: {
  id?: string;
  label: string;
  value: T;
  options: Array<WorkbenchSelectOption<T>>;
  onChange: (value: T) => void;
  describedBy?: string;
  invalid?: boolean;
  children?: ReactNode;
}) {
  const generatedId = useId();
  const selectId = id ?? `polymer-md-select-${generatedId.replace(/:/g, "")}`;
  // A historical choice must remain visible until the user explicitly changes it.
  const compatibleOptions = value && !options.some(option => option.value === value)
    ? [{ value, label: `已保存的选择：${value}` }, ...options]
    : options;
  return <div className="np-polymer-md-field np-pmd-select-field">
    <label htmlFor={selectId}>{label}</label>
    <WorkbenchSelect id={selectId} ariaLabel={label} value={value} options={compatibleOptions}
      onChange={onChange} ariaDescribedBy={describedBy} ariaInvalid={invalid} constrainToScrollParent />
    {children}
  </div>;
}
