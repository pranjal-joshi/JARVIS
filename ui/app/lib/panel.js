// Props that turn a panel heading into the panel's expand / collapse control.
export function headProps(pinned, onToggle, label) {
  return {
    role: "button",
    tabIndex: 0,
    "aria-expanded": pinned,
    title: pinned ? `Collapse ${label} (Esc)` : `Expand ${label}`,
    onClick: onToggle,
    onKeyDown: (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onToggle();
      }
    },
  };
}
