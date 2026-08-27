# saas-demo-charts

Standalone demo Helm charts, one per directory under `charts/`.

## charts/deployment-demo

No custom image — `nginx:alpine` plus a small mounted shell script that
renders `index.html` once at container start.

Shows the everyday Deployment building blocks in one chart:

- **Requests/limits** — CPU/memory set in `values.yaml`
  (`resources.requests`/`resources.limits`), and echoed back on the
  rendered page via the Downward API (`resourceFieldRef`), so you see what
  the container actually got, not just what was asked for.
- **PVC** — `/data` is a PersistentVolumeClaim (`persistence.*` in
  `values.yaml`; `persistence.enabled: false` swaps it for an `emptyDir`).
  A visit counter file lives there: kill the pod, refresh the page, and
  the count keeps climbing instead of resetting to 1 — proof the volume,
  not the container filesystem, held the state.
- **Ingress** — `ingress.*`: className, host, annotations, optional TLS.
- **env** — anything under `values.env` becomes `DEMO_<KEY>` on the
  container and shows up in a table on the page — `--set env.plan=pro`
  and refresh to see it appear.

Local smoke test (no cluster needed):

```bash
helm lint charts/deployment-demo
helm template demo charts/deployment-demo -n demo \
  --set env.plan=pro --set ingress.host=demo.example.com
```
