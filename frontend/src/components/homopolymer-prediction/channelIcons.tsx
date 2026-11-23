import { Thermometer, UnfoldHorizontal, Wind, type LucideIcon } from "lucide-react";
import type { PredictPropertyGroup } from "../../constants/predictableProperties";

/**
 * One icon per property group, chosen from the Lucide set (searched through Streamline) so they share
 * the platform's 2px outline: a thermometer for thermal, arrows pulling apart for mechanical (tensile),
 * and moving air for gas permeation.
 */
export const PROPERTY_GROUP_ICONS: Record<PredictPropertyGroup, LucideIcon> = {
  thermal: Thermometer,
  mechanical: UnfoldHorizontal,
  permeability: Wind
};

/** A small tinted tile in the group's channel colour; decorative, the group name carries the meaning. */
export function ChannelBadge({ group }: { group: PredictPropertyGroup }) {
  const Icon = PROPERTY_GROUP_ICONS[group];
  return (
    <span className="np-hp-channel-badge" aria-hidden="true">
      <Icon />
    </span>
  );
}
