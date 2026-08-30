import { createRoot } from "react-dom/client";

import { MissionControl } from "./MissionControl";
import "./styles.css";

const root = document.getElementById("root");
if (root === null) throw new Error("FleetWatch root element is missing.");

createRoot(root).render(<MissionControl />);
