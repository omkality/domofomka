import { LikeC4View, type LikeC4ViewId } from "likec4:react";
import { useEffect, useState } from "react";

export default function C4Diagram({ viewId, variant = "diagram" }: { viewId: LikeC4ViewId; variant?: "diagram" | "sequence" }) {
  const [scheme, setScheme] = useState<"light" | "dark">("dark");
  useEffect(() => {
    const update = () => setScheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return <LikeC4View key={`${viewId}-${variant}`} viewId={viewId} dynamicViewVariant={variant} colorScheme={scheme} browser enableNotations
    enableElementDetails enableRelationshipDetails enableDynamicViewWalkthrough={variant === "diagram"} />;
}
