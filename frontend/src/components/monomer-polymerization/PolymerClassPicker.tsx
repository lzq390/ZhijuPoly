import { ChevronDown, Layers3 } from "lucide-react";
import {
  type KeyboardEvent,
  useEffect,
  useId,
  useRef,
  useState
} from "react";
import type { MonomerPolymerizationTargetClass } from "../../types";

export type PolymerClassPickerOption = {
  value: MonomerPolymerizationTargetClass;
  label: string;
  monomerCount: string;
  monomerBRequired: boolean;
};

type PolymerClassPickerProps = {
  value: MonomerPolymerizationTargetClass;
  options: PolymerClassPickerOption[];
  onChange: (value: MonomerPolymerizationTargetClass) => void;
};

function nextOptionIndex(current: number, offset: number, length: number) {
  return (current + offset + length) % length;
}

export function PolymerClassPicker({
  value,
  options,
  onChange
}: PolymerClassPickerProps) {
  const pickerRef = useRef<HTMLDivElement>(null);
  const labelId = useId();
  const listboxId = useId();
  const [open, setOpen] = useState(false);
  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  const selectedOption = options[selectedIndex] ?? options[0];

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePress);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePress);
  }, [open]);

  useEffect(() => {
    if (!open) setActiveIndex(selectedIndex);
  }, [open, selectedIndex]);

  function openPicker() {
    setActiveIndex(selectedIndex);
    setOpen(true);
  }

  function chooseOption(index: number) {
    const next = options[index];
    if (!next) return;
    onChange(next.value);
    setActiveIndex(index);
    setOpen(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (!options.length) return;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) openPicker();
        else setActiveIndex((current) => nextOptionIndex(current, 1, options.length));
        break;
      case "ArrowUp":
        event.preventDefault();
        if (!open) openPicker();
        else setActiveIndex((current) => nextOptionIndex(current, -1, options.length));
        break;
      case "Home":
        if (!open) return;
        event.preventDefault();
        setActiveIndex(0);
        break;
      case "End":
        if (!open) return;
        event.preventDefault();
        setActiveIndex(options.length - 1);
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        if (open) chooseOption(activeIndex);
        else openPicker();
        break;
      case "Escape":
        if (!open) return;
        event.preventDefault();
        setOpen(false);
        break;
      case "Tab":
        setOpen(false);
        break;
      default:
        break;
    }
  }

  if (!selectedOption) return null;

  return (
    <div ref={pickerRef} className={`np-mp-field np-mp-class-picker${open ? " is-open" : ""}`}>
      <span id={labelId}>POLYMER CLASS</span>
      <button
        type="button"
        className="np-mp-class-picker__trigger"
        role="combobox"
        aria-labelledby={`${labelId} ${listboxId}-value`}
        aria-controls={listboxId}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-activedescendant={open ? `${listboxId}-option-${activeIndex}` : undefined}
        onClick={() => open ? setOpen(false) : openPicker()}
        onKeyDown={handleKeyDown}
      >
        <span className="np-mp-class-picker__icon" aria-hidden="true"><Layers3 /></span>
        <span className="np-mp-class-picker__selection">
          <strong id={`${listboxId}-value`}>{selectedOption.label}</strong>
          <small>
            {selectedOption.monomerCount}
            <i aria-hidden="true" />
            {selectedOption.monomerBRequired ? "单体 B 必填" : "单体 B 可选"}
          </small>
        </span>
        <ChevronDown className="np-mp-class-picker__chevron" aria-hidden="true" />
      </button>

      {open ? (
        <div id={listboxId} className="np-mp-class-picker__list" role="listbox" aria-labelledby={labelId}>
          {options.map((option, index) => (
            <div
              id={`${listboxId}-option-${index}`}
              key={option.value}
              className={`np-mp-class-picker__option${activeIndex === index ? " is-active" : ""}`}
              role="option"
              aria-selected={option.value === value}
              onMouseEnter={() => setActiveIndex(index)}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => chooseOption(index)}
            >
              <span>
                <strong>{option.label}</strong>
                <small>{option.monomerCount} · {option.monomerBRequired ? "B 必填" : "B 可选"}</small>
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
