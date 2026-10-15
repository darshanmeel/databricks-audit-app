import React from "react";
import { createRoot } from "react-dom/client";

import "./styles/fonts/fonts.css";
import "./styles/styles.css";
import "./styles/names.css";
import "./styles/css/c_shell.css";
import "./styles/css/p_overview_waste.css";
import "./styles/css/p_cost_mlai.css";
import "./styles/css/p_compute_jobs.css";
import "./styles/css/p_queries_gov_storage.css";
import "./styles/css/p_findings_coverage.css";
import "./styles/css/p_guide.css";
import "./styles/css/p_findings_detail.css";
import "./styles/css/p_money.css";
import "./styles/css/p_actions.css";
import "./styles/css/c_exec.css";
import "./styles/css/c_parts.css";

// Loaded for what they do on load: every tab registers its pages with AreaContent, and the
// job panel mounts its own overlay. Listed in full so no page depends on a borrowed helper.
import "./tabs/tab_actions";
import "./tabs/tab_compute";
import "./tabs/tab_cost";
import "./tabs/tab_cost_before_after";
import "./tabs/tab_coverage";
import "./tabs/tab_findings";
import "./tabs/tab_genie";
import "./tabs/tab_governance";
import "./tabs/tab_jobs";
import "./tabs/tab_mlai";
import "./tabs/tab_money";
import "./tabs/tab_overview";
import "./tabs/tab_query";
import "./tabs/query_trend";
import "./tabs/tab_storage";
import "./tabs/tab_tags";
import "./tabs/tab_waste";
import "./components/job_panel";
import App from "./App";

createRoot(document.getElementById("root")!).render(<App />);
