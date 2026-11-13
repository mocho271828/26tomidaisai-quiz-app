import "../../styles/tommy.css";

export type TommyMascotVariant =
  | "idle"
  | "waiting"
  | "thinking"
  | "celebrating"
  | "surprised"
  | "greeting"
  | "running"
  | "hurrying"
  | "applauding"
  | "smirking";

export type TommyMascotSize = "small" | "medium" | "large";

type TommyMascotProps = {
  label?: string;
  size: TommyMascotSize;
  variant: TommyMascotVariant;
};

export function TommyMascot({ label, size, variant }: TommyMascotProps) {
  const className = `tommy-mascot tommy-mascot--${variant} tommy-mascot--${size}`;

  if (label === undefined || label.trim().length === 0) {
    return <span aria-hidden="true" className={className} />;
  }

  return <span aria-label={label} className={className} role="img" />;
}
