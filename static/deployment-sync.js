(function exposeDeploymentSync(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AzureOpenAIDeploymentSync = api;
}(typeof globalThis === "object" ? globalThis : this, function deploymentSyncFactory() {
  "use strict";

  const paths = new Set(["/responses", "/chat/completions", "/completions", "/embeddings"]);
  const usesDeployment = (path, method) => paths.has(path) && method?.toLowerCase() === "post";
  const replaceModel = (body, deployment) => {
    if (!deployment || typeof body !== "string") return body;
    let value;
    try {
      value = JSON.parse(body);
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      // Leave incomplete editor input intact so users can finish editing JSON.
      return body;
    }
    if (!value || Array.isArray(value) || typeof value !== "object" || value.model === deployment) {
      return body;
    }
    return JSON.stringify({ ...value, model: deployment }, null, 2);
  };

  const createDeploymentSync = (getDeployment) => {
    const listeners = new Set();
    const notify = () => listeners.forEach((listener) => listener(getDeployment()));
    const plugin = (system) => ({
      wrapComponents: {
        RequestBody: (Original) => function DeploymentRequestBody(props) {
          const { React } = system;
          const [deployment, setDeployment] = React.useState(getDeployment);
          React.useEffect(() => {
            listeners.add(setDeployment);
            setDeployment(getDeployment());
            return () => listeners.delete(setDeployment);
          }, []);
          const path = props.specPath.get(1);
          const method = props.specPath.get(2);
          const enabled = usesDeployment(path, method) && Boolean(deployment);
          const requestBody = React.useMemo(() => {
            if (!enabled) return props.requestBody;
            const example = props.requestBody.getIn(["content", "application/json", "example"]);
            if (!example?.set) return props.requestBody;
            return props.requestBody.setIn(
              ["content", "application/json", "example"], example.set("model", deployment),
            );
          }, [props.requestBody, enabled, deployment]);
          const value = enabled
            ? replaceModel(props.requestBodyValue, deployment)
            : props.requestBodyValue;
          React.useEffect(() => {
            if (value !== props.requestBodyValue) props.onChange(value);
          }, [value, props.requestBodyValue, props.onChange]);
          return React.createElement(Original, {
            ...props, requestBody, requestBodyValue: value,
          });
        },
      },
    });
    return { plugin, notify };
  };

  return { usesDeployment, replaceModel, createDeploymentSync };
}));
