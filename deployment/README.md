# Despliegue en Azure Container Apps

[deploy.sh](./deploy.sh) crea un resource group si no existe, un entorno de
Container Apps y dos aplicaciones independientes con las imagenes publicadas en
GHCR. No crea Foundry, proyectos, modelos ni agentes remotos.

Cada app usa su **identidad administrada de sistema** con
`AUTH_MODE=managed_identity`. El script asigna **Foundry User**
(`53ca6127-db72-4b80-b1b0-d745d6d5456d`, anteriormente Azure AI User) a ambas
identidades en el ambito de la **cuenta Foundry existente**. Ese ambito permite
consumir sus despliegues y hereda permisos hacia sus proyectos; no se asignan
roles en toda la suscripcion. No se usan API keys ni secretos de registro.

## Requisitos

- Bash 4+, Azure CLI, extension `containerapp` y curl 7.71+.
- Sesion `az login` en el tenant de Foundry y Container Apps.
- Permisos para registrar los proveedores `Microsoft.App` y
  `Microsoft.OperationalInsights`, crear los recursos y asignar Foundry User en
  la cuenta (por ejemplo, Contributor para los recursos y Role Based Access
  Control Administrator en la cuenta Foundry).
- Proyecto Foundry existente con ambos despliegues de modelo. El endpoint y el
  resource ID deben corresponder a la misma cuenta. La cuenta debe permitir
  acceso de red desde Container Apps; este script no configura VNet, DNS privado
  ni private endpoints.
- Ambas imagenes de GHCR deben ser **publicas** y compatibles con Linux/amd64.
  Publicarlas desde GitHub Actions no hace publicos los paquetes automaticamente:
  configura su visibilidad en GitHub Packages. No se admite GHCR privado.

## Ejecutar

Desde la raiz del repositorio, despues de publicar ambas imagenes:

```bash
az login
az extension add --name containerapp --upgrade

export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export RESOURCE_GROUP="rg-ai-cost-control"
export LOCATION="swedencentral"
export FOUNDRY_RESOURCE_ID="/subscriptions/<foundry-subscription-id>/resourceGroups/<foundry-rg>/providers/Microsoft.CognitiveServices/accounts/<foundry-account>"
export FOUNDRY_PROJECT_ENDPOINT="https://<foundry-account>.services.ai.azure.com/api/projects/<project>"
export RISK_IMAGE="ghcr.io/<owner>/<repo>-risk:<tag>"
export CUSTOMER_IMAGE="ghcr.io/<owner>/<repo>-customer:<tag>"

# Opcionales: deben ser nombres de despliegues existentes en Foundry.
export RISK_MODEL_DEPLOYMENT="gpt-5.6-sol"
export CUSTOMER_MODEL_DEPLOYMENT="gpt-5.4-mini"

bash deployment/deploy.sh
```

Usa tags inmutables por SHA o digests para despliegues reproducibles.
`bash deployment/deploy.sh --help` muestra los nombres configurables del entorno
y las apps. El script no carga `.env`, no cambia la suscripcion predeterminada
de Azure CLI y detiene la ejecucion ante errores. Se puede volver a ejecutar:
reutiliza el entorno, actualiza las apps y no duplica las asignaciones existentes.
No mueve apps que ya pertenezcan a otro entorno.

## Acceso, estado y comprobacion

**El ingress HTTPS es externo y publico, sin autenticacion de clientes.**
Cualquiera que conozca las URLs puede invocar los agentes y generar consumo
facturable. Es una configuracion de demostracion elegida expresamente; utiliza
solo datos sinteticos y protege el acceso con autenticacion, cuotas y limites
antes de un uso real. La identidad administrada autentica la app ante Foundry,
**no** al cliente ante la app.

Las URLs HTTPS se muestran al terminar y se configuran en
`RISK_PUBLIC_URL` / `CUSTOMER_PUBLIC_URL` para las Agent Cards A2A.
El script verifica `/healthz` con reintentos; no ejecuta inferencia ni carga.
Los permisos de Foundry pueden tardar varios minutos en propagarse: un health
check correcto no confirma que la identidad pueda consumir el modelo.

Puedes comprobar A2A y Foundry con una solicitud real (genera consumo):

```bash
uv run python -m clients.risk --url "https://<risk-fqdn>" \
  --prompt "Analiza los riesgos de una empresa ficticia"
uv run python -m clients.customer --url "https://<customer-fqdn>" \
  --prompt "He perdido mi tarjeta, que pasos debo seguir?"
```

Cada app tiene 0.5 vCPU, 1 GiB y una sola replica, con revisiones en modo single.
No se escala horizontalmente porque el almacen de tareas A2A es en memoria.
Los reinicios y las nuevas revisiones pierden ese estado. Una replica minima
genera coste aun sin peticiones. El entorno no crea Log Analytics ni conserva
logs de forma centralizada (`--logs-destination none`); para diagnosticar usa
`az containerapp logs show --resource-group "$RESOURCE_GROUP" --name risk-agent --follow`.
El ingress HTTP de Container Apps tiene sus propios limites de timeout: no
garantiza streaming de duracion ilimitada.

Las asignaciones de rol en Foundry sobreviven al borrado del resource group de
Container Apps. Si retiras el despliegue, elimina tambien esas asignaciones
usando los principal IDs mostrados por el script.
