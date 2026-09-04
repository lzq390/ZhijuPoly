import { Check, ChevronDown } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent
} from "react";

export type MonomerDftSelectOption<T extends string> = {
  value: T;
  label: string;
  description?: string;
  disabled?: boolean;
};

type MonomerDftSelectProps<T extends string> = {
  id: string;
  value: T;
  options: Array<MonomerDftSelectOption<T>>;
  onChange: (value: T) => void;
  disabled?: boolean;
  ariaLabel?: string;
  ariaLabelledBy?: string;
};

export function MonomerDftSelect<T extends string>({
  id,
  value,
  options,
  onChange,
  disabled = false,
  ariaLabel,
  ariaLabelledBy
}: MonomerDftSelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [opensUp, setOpensUp] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selectedOption = options[selectedIndex] ?? options[0];
  const listboxId = `${id}-listbox`;

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    document.getElementById(`${id}-option-${activeIndex}`)?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, id, open]);

  function firstEnabledIndex(direction: 1 | -1, fromIndex?: number): number {
    if (options.length === 0) return -1;
    const start = fromIndex ?? (direction === 1 ? -1 : options.length);
    for (let offset = 1; offset <= options.length; offset += 1) {
      const index = (start + direction * offset + options.length) % options.length;
      if (!options[index]?.disabled) return index;
    }
    return -1;
  }

  function openMenu(direction: 1 | -1 = 1) {
    if (disabled || options.length === 0) return;
    const triggerBounds = triggerRef.current?.getBoundingClientRect();
    if (triggerBounds) {
      const availableBelow = window.innerHeight - triggerBounds.bottom;
      setOpensUp(availableBelow < 300 && triggerBounds.top > availableBelow);
    }
    const preferred = selectedIndex >= 0 && !options[selectedIndex]?.disabled
      ? selectedIndex
      : firstEnabledIndex(direction);
    setActiveIndex(preferred);
    setOpen(true);
  }

  function closeMenu(restoreFocus = true) {
    setOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus());
  }

  function choose(index: number) {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    closeMenu();
  }

  function handleTriggerKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Tab" && open) {
      setOpen(false);
      return;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if ((event.key === "Enter" || event.key === " ") && open) {
      event.preventDefault();
      if (activeIndex >= 0) choose(activeIndex);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const direction = event.key === "ArrowUp" || event.key === "End" ? -1 : 1;
      if (!open) {
        openMenu(direction);
        return;
      }
      const nextIndex = event.key === "Home"
        ? firstEnabledIndex(1)
        : event.key === "End"
          ? firstEnabledIndex(-1)
          : firstEnabledIndex(direction, activeIndex);
      if (nextIndex >= 0) setActiveIndex(nextIndex);
    }
  }

  function handleOptionKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu();
      return;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      choose(index);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const nextIndex = event.key === "Home"
      ? firstEnabledIndex(1)
      : event.key === "End"
        ? firstEnabledIndex(-1)
        : firstEnabledIndex(event.key === "ArrowDown" ? 1 : -1, index);
    if (nextIndex >= 0) setActiveIndex(nextIndex);
  }

  return (
    <div ref={rootRef} className={`np-dft-select${open ? " is-open" : ""}${opensUp ? " opens-up" : ""}`}>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        className="np-dft-select__trigger"
        role="combobox"
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledBy ? `${ariaLabelledBy} ${id}-value` : undefined}
        aria-controls={listboxId}
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-activedescendant={open && activeIndex >= 0 ? `${id}-option-${activeIndex}` : undefined}
        disabled={disabled}
        onClick={() => open ? closeMenu(false) : openMenu()}
        onKeyDown={handleTriggerKeyDown}
      >
        <span id={`${id}-value`}>
          <strong>{selectedOption?.label ?? "请选择"}</strong>
          {selectedOption?.description ? <small>{selectedOption.description}</small> : null}
        </span>
        <ChevronDown aria-hidden="true" />
      </button>

      {open ? (
        <div id={listboxId} className="np-dft-select__menu" role="listbox" aria-labelledby={ariaLabelledBy}>
          {options.map((option, index) => {
            const selected = option.value === value;
            return (
              <button
                key={option.value || "empty"}
                id={`${id}-option-${index}`}
                type="button"
                role="option"
                aria-selected={selected}
                aria-disabled={option.disabled || undefined}
                tabIndex={-1}
                disabled={option.disabled}
                className={`${selected ? "is-selected" : ""}${activeIndex === index ? " is-active" : ""}`}
                onMouseEnter={() => { if (!option.disabled) setActiveIndex(index); }}
                onClick={() => choose(index)}
                onKeyDown={(event) => handleOptionKeyDown(event, index)}
              >
                <span>
                  <strong>{option.label}</strong>
                  {option.description ? <small>{option.description}</small> : null}
                </span>
                {selected ? <Check aria-hidden="true" /> : null}
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
