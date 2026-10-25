import { defineRailway, github, project, service } from "railway/iac";
// This repository manages only its own resources in the environment. Other
// repositories export their own partial name.
// See https://docs.railway.com/infrastructure-as-code#multi-repo-projects
export const partial = "Riftwatcher-Poro";
export default defineRailway(() => {
  const RiftwatcherPoro = service("Riftwatcher-Poro", {
    source: github("NoxVain/Riftwatcher-Poro", { branch: "main", checkSuites: false }),
    build: { buildEnvironment: "V3", builder: "RAILPACK" },
    start: "python -m src.app",
    // Carried over from railway.json; `railway config migrate` does not translate it.
    // No restartPolicyMaxRetries: Railway only keeps it for ON_FAILURE.
    deploy: { restartPolicyType: "ALWAYS" },
    replicas: { "us-east4-eqdc4a": 1 },
    networking: { privateNetworkEndpoint: "riftwatcher-poro" },
  });

  return project("Riftwatcher Poro", {
    variables: { managed: false },
    resources: [RiftwatcherPoro],
  });
});
