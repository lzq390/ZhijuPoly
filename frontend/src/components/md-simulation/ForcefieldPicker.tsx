import { ChevronDown, Gauge } from "lucide-react";
import {
  type KeyboardEvent,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { MD_DEMO_FORCEFIELD_OPTIONS } from "./config";

type ForcefieldPickerProps = {
  value: string;
  error?: string;
  onChange: (value: string) => void;
  onTouched: () => void;
};

function nextOptionIndex(current: number, offset: number, length: number) {
  return (current + offset + length) % length;
}

export function ForcefieldPicker({
  value,
  error,
  onChange,
  onTouched,
}: ForcefieldPickerProps) {
  const pickerRef = useRef<HTMLDivElement>(null);
  const labelId = useId();
  const listboxId = useId();
  const errorId = useId();
  const options = useMemo(() => {
    if (!value || MD_DEMO_FORCEFIELD_OPTIONS.some((option) => option.value === value)) {
      return [...MD_DEMO_FORCEFIELD_OPTIONS];
    }
    return [
      { value, label: value, hint: "已保存的力场" },
      ...MD_DEMO_FORCEFIELD_OPTIONS,
    ];
  }, [value]);
  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const selectedOption = options[selectedIndex] ?? options[0];
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(selectedIndex);

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePress = (event: PointerEvent) => {
      if (pickerRef.current?.contains(event.target as Node)) return;
      setOpen(false);
      onTouched();
    };
    document.addEventListener("pointerdown", closeOnOutsidePress);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePress);
  }, [onTouched, open]);

  useEffect(() => {
    if (!open) setActiveIndex(selectedIndex);
  }, [open, selectedIndex]);

  function openPicker() {
    setActiveIndex(selectedIndex);
    setOpen(true);
  }

  function chooseOption(index: number) {
    const option = options[index];
    if (!option) return;
    onChange(option.value);
    onTouched();
    setActiveIndex(index);
    setOpen(false);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (!options.length) return;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!open) openPicker();
        else
          setActiveIndex((current) =>
            nextOptionIndex(current, 1, options.length),
          );
        break;
      case "ArrowUp":
        event.preventDefault();
        if (!open) openPicker();
        else
          setActiveIndex((current) =>
            nextOptionIndex(current, -1, options.length),
          );
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
        onTouched();
        break;
      case "Tab":
        setOpen(false);
        onTouched();
        break;
      default:
        break;
    }
  }

  if (!selectedOption) return null;

  return (
    <div
      ref={pickerRef}
      className={`np-md-field np-md-forcefield-picker${open ? " is-open" : ""}${error ? " has-error" : ""}`}
    >
      <span id={labelId}>力场</span>
      <div className="np-md-forcefield-picker__control">
        <button
          id="md-forcefield"
          type="button"
          className="np-md-forcefield-picker__trigger"
          role="combobox"
          aria-labelledby={`${labelId} ${listboxId}-value`}
          aria-describedby={error ? errorId : undefined}
          aria-controls={listboxId}
          aria-expanded={open}
          aria-haspopup="listbox"
          aria-invalid={error ? true : undefined}
          aria-activedescendant={
            open ? `${listboxId}-option-${activeIndex}` : undefined
          }
          onClick={() => (open ? setOpen(false) : openPicker())}
          onKeyDown={handleKeyDown}
        >
          <span className="np-md-forcefield-picker__icon" aria-hidden="true">
            <Gauge />
          </span>
          <span className="np-md-forcefield-picker__selection">
            <strong id={`${listboxId}-value`}>{selectedOption.label}</strong>
            <small>{selectedOption.hint}</small>
          </span>
          <ChevronDown
            className="np-md-forcefield-picker__chevron"
            aria-hidden="true"
          />
        </button>

        {open ? (
          <div
            id={listboxId}
            className="np-md-forcefield-picker__list"
            role="listbox"
            aria-labelledby={labelId}
          >
            {options.map((option, index) => (
              <div
                id={`${listboxId}-option-${index}`}
                key={option.value}
                className={`np-md-forcefield-picker__option${activeIndex === index ? " is-active" : ""}`}
                role="option"
                aria-selected={option.value === value}
                onMouseEnter={() => setActiveIndex(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => chooseOption(index)}
              >
                <span>
                  <strong>{option.label}</strong>
                  <small>{option.hint} · 用于本次模拟</small>
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </div>
      {error ? (
        <em id={errorId} role="alert">
          {error}
        </em>
      ) : null}
    </div>
  );
}
