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
- Cuenta Foundry / Azure OpenAI existente con ambos despliegues de modelo y un
  endpoint OpenAI v1, directo o publicado mediante APIM. No se necesita un
  proyecto Foundry. El resource ID identifica la cuenta donde se asignan los
  permisos; una URL de APIM no permite deducir ese ambito. El endpoint debe ser
  accesible desde Container Apps; este script no configura VNet, DNS privado ni
  private endpoints.
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
export OPENAI_BASE_URL="https://finops4ai.azure-api.net/finopsforai/openai/v1"
# Alternativa directa:
# export OPENAI_BASE_URL="https://finopsforai.openai.azure.com/openai/v1"
export RISK_IMAGE="ghcr.io/<owner>/<repo>-risk:<tag>"
export CUSTOMER_IMAGE="ghcr.io/<owner>/<repo>-customer:<tag>"

# Opcionales: deben ser nombres de despliegues existentes en Foundry.
export RISK_MODEL_DEPLOYMENT="gpt-5.6-sol"
export CUSTOMER_MODEL_DEPLOYMENT="gpt-5.4-mini"

# RISK_OPENAI_BASE_URL y CUSTOMER_OPENAI_BASE_URL sobrescriben el destino por app.

bash deployment/deploy.sh
```

Usa tags inmutables por SHA o digests para despliegues reproducibles.
`bash deployment/deploy.sh --help` muestra los nombres configurables del entorno
y las apps. El script no carga `.env`, no cambia la suscripcion predeterminada
de Azure CLI y detiene la ejecucion ante errores. Se puede volver a ejecutar:
reutiliza el entorno, actualiza las apps y no duplica las asignaciones existentes.
No mueve apps que ya pertenezcan a otro entorno.

El script pasa las URLs base OpenAI a las apps tanto al crearlas como al
actualizarlas. Los overrides por app no definidos o vacios usan la URL compartida;
si no hay URL compartida, se necesitan ambos overrides. No hay un endpoint
implicito ni una dependencia del SDK de proyectos. Mantiene Azure Identity y el scope
`https://ai.azure.com/.default`: APIM debe validar esa audiencia y configurar
su autenticacion hacia Foundry. No configura las politicas de APIM.

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
RISK_AGENT_CARD_URL="https://<risk-fqdn>/.well-known/agent-card.json" \
  uv run python -m clients.risk \
  --prompt "Analiza los riesgos de una empresa ficticia"
CUSTOMER_AGENT_CARD_URL="https://<customer-fqdn>/.well-known/agent-card.json" \
  uv run python -m clients.customer \
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

## Usuarios y subscriptions de APIM

[onboard_apim_users.py](onboard_apim_users.py) da de alta usuarios del Developer
Portal a partir de un CSV sin cabecera `department,team,email`. Para cada
usuario nuevo establece:

- `firstName`: parte del email anterior a `@`.
- `lastName`: `Demo`.
- `email`: valor del CSV.
- `state`: `active`.

Con `--subscriptions "premium, standard, free"` crea **una subscription por
usuario**, asociada aleatoriamente a uno de esos **IDs de productos existentes**.
No crea productos ni les anade APIs. Las subscriptions se crean activas y APIM
genera sus claves. El script recupera la **primaryKey** y la guarda como cuarta
columna en un **nuevo fichero con sufijo `.subs`**. Por ejemplo,
`identities.csv` produce `identities.csv.subs`; el original no se modifica:

```text
department,team,email,subscription_key
```

La linea anterior describe el formato: **no anadas una fila de cabecera**.
Las filas duplicadas para un mismo email comparten usuario, subscription y clave,
pero conservan sus departamentos y equipos.

### Ejecutar

Requiere una sesion `az login`, Azure CLI y permisos de administracion de
usuarios/subscriptions sobre APIM, incluido `listSecrets` (por ejemplo,
API Management Service Contributor). Se ejecuta desde la raiz del repositorio:

```bash
export AZURE_SUBSCRIPTION_ID="<subscription-id>"
export RESOURCE_GROUP="finops-for-ai-rg"
export APIM_NAME="finops4ai"

# Revisar sin crear recursos, recuperar claves ni modificar el CSV.
uv run python -m deployment.onboard_apim_users \
  clients/data/identities.csv \
  --subscriptions "premium, standard, free" --seed 42 --dry-run

# Crear usuarios/subscriptions y generar identities.csv.subs con las claves.
uv run python -m deployment.onboard_apim_users \
  clients/data/identities.csv \
  --subscriptions "premium, standard, free" --seed 42

# Usar las identidades y la clave de cada fila en las peticiones al agente.
uv run python -m clients.customer --prompt "He perdido mi tarjeta" \
  --identities-file clients/data/identities.csv.subs
```

Alternativamente, sustituye las variables por `--azure-subscription-id`,
`--resource-group` y `--service-name`. El script no carga `.env`.
`--seed` hace reproducible la seleccion para el mismo conjunto de usuarios
nuevos y productos. Sin seed, la seleccion no es determinista y no garantiza
un reparto equilibrado.

Para crear **solo usuarios**, omite `--subscriptions`; en ese caso no consulta
subscriptions, recupera claves ni genera el fichero `.subs`.

### Reejecuciones y errores

- Valida el CSV completo y la existencia de todos los productos antes de
  escribir recursos en Azure.
- Reutiliza usuarios existentes por email, sin distinguir mayusculas. No
  cambia sus nombres; si no estan activos, falla antes de escribir para no
  reactivar cuentas bloqueadas de forma implicita.
- Utiliza IDs estables derivados del email. Reutiliza la subscription creada
  por este script sin cambiar producto, estado ni claves, aunque cambien
  las opciones de `--subscriptions`. No reutiliza arbitrariamente otras
  subscriptions del mismo usuario.
- En cada ejecucion con subscriptions recupera la clave primaria actual:
  si fue rotada externamente, actualiza la cuarta columna del fichero `.subs`.
- Una subscription existente suspendida se conserva suspendida y se muestra
  su estado; el script no la reactiva. Su clave no autoriza llamadas mientras
  siga suspendida.
- Guarda el fichero `.subs` atomicamente despues de completar cada usuario. En Linux,
  los archivos escritos quedan con permisos `0600`. La cuarta columna puede
  quedar vacia en usuarios pendientes si una ejecucion se interrumpe.
- Ante un fallo termina con codigo 1; no revierte usuarios o subscriptions
  ya creados. Las claves guardadas previamente permanecen y una reejecucion
  permite continuar. Conserva las claves anteriores de los usuarios pendientes
  cuando ya existe un fichero de salida. Si no pudo guardar una clave, puede recuperarla al
  reejecutar sin crear otra subscription.
- Rechaza enlaces simbolicos y evita sobrescribir un fichero de destino que haya cambiado
  durante la ejecucion. No ejecutes dos procesos de onboarding sobre el mismo
  fichero simultaneamente.

### Seguridad y acceso

El fichero `.subs` resultante **contiene secretos**. No se muestran claves en
consola. El CSV original, incluido el ejemplo versionado
`clients/data/identities.csv`, permanece intacto.
Los ficheros `*.csv.subs` estan ignorados por Git. Si la entrada no tiene
extension `.csv`, excluye tambien su salida `<nombre>.subs` del control de versiones. En Windows,
comprueba las ACL: `0600` no sustituye a los permisos de Windows.

La clave se envia como `Ocp-Apim-Subscription-Key` tanto al runtime, por
peticion, como a la descarga de la card usando la identidad de la primera
peticion. Los productos deben contener las APIs necesarias y estas deben
exigir subscription key para que se aplique esa proteccion.
Los agentes propagan tambien la misma clave al endpoint del modelo configurado.
Si ese endpoint es otra API de APIM con subscription obligatoria, incluye esa
API en el producto correspondiente. Las llamadas A2A y al modelo son peticiones
distintas y pueden consumir cada una los contadores del producto.

Estos usuarios pertenecen a APIM, no a Microsoft Entra ID. El script no crea
passwords conocidos, configura proveedores de identidad, envia invitaciones
ni garantiza inicio de sesion interactivo en el portal. Tampoco configura
policies de atribucion, cuotas, rate limits o autenticacion de cabeceras
`X-Department`, `X-Team` y `X-User`.
