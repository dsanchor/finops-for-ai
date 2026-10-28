# FinOps for AI

## Simulacion de agentes y perfiles de consumo

Dos servicios Python independientes implementados con **Microsoft Agent Framework**.
Los modelos se consumen mediante `OpenAIChatClient` (Responses API OpenAI v1),
directamente en Azure OpenAI o a traves de APIM. No se necesita un proyecto
Microsoft Foundry. Los servicios no crean agentes remotos ni infraestructura de
Azure; el [script de despliegue](./deployment/README.md) permite aprovisionar
Azure Container Apps de forma explicita.

| Servicio | Departamento ficticio | Despliegue por defecto | Carga sintetica por defecto |
| --- | --- | --- | --- |
| Riesgos | CIB | `gpt-5.6-sol` | 3 solicitudes, concurrencia 1, documentos de 60 secciones |
| Atencion al cliente | Retail | `gpt-5.4-mini` | 100 solicitudes, concurrencia 8, preguntas breves |

Los nombres son **nombres de despliegue**, no una garantia de disponibilidad de esos
modelos en Foundry. Deben existir en la cuenta de destino; cambia las variables si tienen
otros nombres. El perfil premium/mini simula patrones de consumo, pero el coste real
depende del modelo desplegado, los tokens, la cache y las tarifas de Azure.
No se inventan precios ni se infiere el modelo a partir del nombre del despliegue.

## 1. Configuracion

Requisitos: Python 3.12+, [uv](https://docs.astral.sh/uv/), un endpoint OpenAI v1
con los dos despliegues y una identidad con permisos de inferencia (por ejemplo,
**Foundry User**, anteriormente Azure AI User, en el ambito adecuado).

```bash
uv sync --frozen
cp .env.example .env
```

En WSL, si `.venv` dentro de un montaje Windows/OneDrive produce errores de
entrada/salida al instalar dependencias, usa un entorno en el filesystem Linux.
Mantiene el mismo lockfile; conserva esta variable tambien para `uv run`:

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/ai-cost-control"
uv sync --frozen
```

Edita `.env` con la URL base OpenAI v1 y los nombres de despliegue. Usa el
mismo cliente y la misma configuracion tanto para APIM como para Azure OpenAI:

```bash
OPENAI_BASE_URL=https://<apim-endpoint>/finopsforai/openai/v1
# Alternativa directa, sin APIM:
# OPENAI_BASE_URL=https://<foundry-endpoint>/openai/v1
```

`RISK_OPENAI_BASE_URL` y `CUSTOMER_OPENAI_BASE_URL` permiten destinos distintos
por agente. Un override por agente vacio usa la URL compartida. Cada agente debe
tener una URL base, ya sea compartida o propia; si falta, falla al arrancar.
No se usa `FOUNDRY_PROJECT_ENDPOINT` ni se instancia el SDK de proyectos.
Las solicitudes Responses van a `<OPENAI_BASE_URL>/responses`, manteniendo
streaming y las cabeceras `X-Agent`, `X-Department`, `X-Team`, `X-User`,
`Ocp-Apim-Subscription-Key` recibidas.
OpenAI v1 no necesita un parametro `api-version`; las antiguas variables
`API_VERSION` y `ALLOW_PREVIEW` ya no se utilizan.

La autenticacion usa Azure Identity y un proveedor de tokens Entra ID, con scope
`https://ai.azure.com/.default`; cambiar la URL no cambia la audiencia del token.
APIM debe aceptar y validar esa audiencia y configurar la autenticacion hacia
Foundry. No se anaden API keys del modelo ni se reenvia el token del cliente A2A.
La cabecera `Ocp-Apim-Subscription-Key` recibida se propaga al endpoint del modelo,
pero no se registra en logs. No se propagan `X-Product-Id` ni `X-Subscription-Id`:
APIM obtiene ese contexto al validar la clave. Para reutilizarla, las APIs del
agente y del modelo deben pertenecer al mismo producto de la misma instancia
APIM. Si vas a enviar claves, configura la URL del modelo para apuntar a ese
gateway de confianza; el agente las reenvia al destino configurado, sin detectar
automaticamente si es APIM o Foundry. Una audiencia Entra distinta requiere
configuracion de autenticacion adicional.

La autenticacion ante Foundry usa **Azure Identity**, sin API keys del modelo:

- Local: `AUTH_MODE=cli`; autenticate manualmente con `az login`.
- En Azure: `AUTH_MODE=managed_identity`. Asigna una identidad y sus permisos al
  recurso que ejecute cada contenedor. `MANAGED_IDENTITY_CLIENT_ID` selecciona una
  identidad administrada asignada por el usuario; omitela para identidad de sistema.
- `AUTH_MODE=default`: `DefaultAzureCredential`, que permite, entre otros,
  Azure CLI en desarrollo o identidad federada con `AZURE_CLIENT_ID`,
  `AZURE_TENANT_ID` y `AZURE_FEDERATED_TOKEN_FILE`. En produccion se recomienda
  seleccionar la identidad explicitamente.

### Variables de entorno

Las variables exportadas tienen precedencia sobre `.env`. Cada proceso lee el
archivo al arrancar. `.env` y variantes quedan excluidos de Git y del contexto Docker.

| Variable | Uso / valor por defecto |
| --- | --- |
| `OPENAI_BASE_URL` | URL base HTTPS OpenAI v1 compartida, necesaria si no hay override por agente |
| `RISK_OPENAI_BASE_URL`, `CUSTOMER_OPENAI_BASE_URL` | Sobrescriben la URL base por agente; vacias usan la compartida |
| `RISK_MODEL_DEPLOYMENT` | `gpt-5.6-sol` |
| `CUSTOMER_MODEL_DEPLOYMENT` | `gpt-5.4-mini` |
| `AUTH_MODE` | `default` en codigo; `cli` en el ejemplo |
| `MANAGED_IDENTITY_CLIENT_ID` | Opcional |
| `REQUEST_TIMEOUT` | 180 segundos por solicitud HTTP al modelo |
| `MAX_RETRIES` | 0 por defecto: comunica el primer error; un override positivo habilita reintentos |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Opcional: exporta fallos finales saneados a `exceptions` del mismo Application Insights de APIM |
| `APIM_SERVICE_ID` | Fallback del servicio APIM si no llega `X-Apim-Service-Id` |
| `LOG_LEVEL` | `INFO` |
| `RISK_HOST`, `CUSTOMER_HOST` | `0.0.0.0` |
| `RISK_PORT`, `CUSTOMER_PORT` | 8001 / 8002 |
| `RISK_PUBLIC_URL`, `CUSTOMER_PUBLIC_URL` | URL anunciada en la Agent Card, sin barra final |
| `RISK_REASONING_EFFORT`, `CUSTOMER_REASONING_EFFORT` | Opcionales; usa solo valores admitidos por tu despliegue |
| `RISK_AGENT_CARD_URL`, `CUSTOMER_AGENT_CARD_URL` | URL completa obligatoria de la Agent Card para cada cliente |

`AUTH_MODE`, `MANAGED_IDENTITY_CLIENT_ID`,
`REQUEST_TIMEOUT`, `MAX_RETRIES` y `LOG_LEVEL` admiten prefijos `RISK_` y
`CUSTOMER_` para sobrescribir valores por agente. No se establece temperatura
para evitar incompatibilidades con modelos de razonamiento. Las respuestas se
solicitan con `store=false`; cada solicitud tiene una sesion independiente.
Los agentes y clientes no imponen un limite de tokens de salida: no se envia
`max_tokens` ni `max_output_tokens`. Se aplican los valores por defecto y los
maximos del modelo y de Foundry, por lo que la salida no es ilimitada ni se
garantiza que el proveedor reserve todo el contexto disponible. Las antiguas
variables `RISK_MAX_OUTPUT_TOKENS` y `CUSTOMER_MAX_OUTPUT_TOKENS` ya no se utilizan.
Esto permite un consumo mayor; se mantienen los timeouts, la concurrencia y el
numero de peticiones configurados.
Reinicia el servicio despues de cambiar su configuracion.

## 2. Ejecutar los servicios

En dos terminales:

```bash
uv run python -m agents.risk
```

```bash
uv run python -m agents.customer
```

Cada servicio publica:

- `GET /healthz`: comprueba que el proceso esta vivo; **no valida permisos ni
  disponibilidad del modelo en Foundry**.
- `GET /.well-known/agent-card.json`: descubrimiento y capacidad streaming.
- `POST /`: A2A **1.0 JSON-RPC**, incluyendo streaming mediante SSE. Se usa el SDK
  oficial A2A y el adaptador `A2AExecutor` de Agent Framework, no un protocolo propio.

  Ambos agentes propagan las cabeceras HTTP `X-Agent`, `X-Department`, `X-Team`,
  `X-User` y `Ocp-Apim-Subscription-Key` de cada solicitud A2A
  a las llamadas al modelo mediante
  `extra_headers` del SDK. Los valores se conservan por
  peticion, tambien en streaming y con solicitudes concurrentes; no se modifica
  el cliente compartido ni se introducen en el prompt. Los nombres de cabecera
  se reconocen sin distinguir mayusculas y minusculas. Si no llega alguna
  cabecera, no se envia ni se inventa un valor a partir del perfil. No se propagan otras
  cabeceras entrantes, incluidos `Authorization`, `X-Product-Id` y `X-Subscription-Id`:
  se mantiene Azure Identity para
  autenticar las llamadas a Foundry.

  Al iniciar cada ejecucion A2A, ambos agentes emiten un log JSON de nivel `INFO`
  con `event=request_headers`, `agent` (el tipo de agente local) y `headers`
  con los valores recibidos de `X-Agent`, `X-Department`, `X-Team` y `X-User`.
  Las cabeceras ausentes se omiten; si solo llega la clave o no llega ninguna,
  `headers` es `{}`.
  No se registra `Ocp-Apim-Subscription-Key`, otras cabeceras, tokens de autenticacion ni prompts en este
  evento. `X-User` puede contener un correo: protege el acceso y la retencion de
  estos logs. Configura `LOG_LEVEL=INFO` (o el override del agente) para verlos.

  Configura APIM para sobrescribir `X-Agent` con el identificador del agente y
  establecer `X-Department`, `X-Team` y `X-User` segun el contexto validado al
  recibir la llamada A2A. Estas cabeceras son metadatos, no una prueba de identidad:
  el agente no las autentica y un cliente con acceso directo puede falsificarlas.
  APIM solo podra usarlas en las metricas del modelo si esas llamadas tambien
  pasan por su gateway; reenviarla no cambia el endpoint de Foundry.

Si cambias puertos, dominio o proxy, configura la URL publica que puedan alcanzar
los clientes. Un proxy debe permitir SSE sin buffering y con timeouts adecuados.
Estos servicios de demostracion **no autentican al cliente A2A**: limita su acceso
a localhost o una red privada. Antes de publicarlos en Internet, protege el acceso
en un gateway con autenticacion, TLS, cuotas y limites de carga.

El almacen de tareas es en memoria y se pierde al reiniciar. No es una
implementacion de conversaciones persistentes ni un servicio distribuido.
Para cargas prolongadas debe sustituirse por un almacen persistente con
politica de retencion; las solicitudes pueden almacenar contenido en ese almacen.
Usa solo datos sinteticos en esta demostracion.

### Errores del modelo, SSE y correlacion

Los errores finales de la ejecucion se comunican como tarea A2A `FAILED`, sin
cambiar el HTTP200 de un SSE ya iniciado. El mensaje se sanea: no contiene
el body original del error, prompts, claves ni tokens. La metadata del evento
incluye `invocation_id`, `model_call_id`, `error_origin`, `error_kind`,
`retryable` y, cuando existen, `upstream_status` y `retry_after_seconds`.
Una HTTP403 se clasifica como `forbidden`, no como cuota, salvo evidencia
explicita como `QuotaExceeded` o `insufficient_quota`.

Con `APPLICATIONINSIGHTS_CONNECTION_STRING` configurada, el agente exporta una
excepcion final por ejecucion fallida a `exceptions`, identificada por
`customDimensions.event = "agent_invocation_failed"` y `schema_version = "1"`.
No se anaden excepciones por cada reintento ni se instrumentan automaticamente
todas las peticiones HTTP del agente. Sin esa variable, los fallos siguen
apareciendo en logs JSON y A2A, pero un aviso al arrancar indica que no se
exportan a Application Insights.

Para enlazar el fallo con la peticion original, configura APIM con el
[fragmento de correlacion](deployment/apim-agent-correlation.xml) y sigue la
[configuracion de despliegue](deployment/README.md#excepciones-y-correlacion-con-apim).
APIM sobrescribe `X-Invocation-Id` con su `context.RequestId` y
`X-Apim-Service-Id` con el servicio. Sin el primer header se genera un UUID:
el error es visible, pero no puede enlazarse automaticamente con requests de APIM.
Un header presente debe contener un UUID valido; no se acepta cualquier texto.
Protege el acceso directo al agente: esos headers no autentican al emisor.

Se reenvian `X-Invocation-Id` y `X-Model-Call-Id` al endpoint del modelo.
El segundo identifica la llamada logica actual y permanece estable si se
habilitan reintentos; cada ejecucion tiene identificadores aislados.
Actualmente cada ejecucion realiza una llamada logica al modelo, sin herramientas.
Si se introducen varias llamadas por ejecucion, habra que asignar un
`model_call_id` distinto a cada una.

Tambien se reenvian, si llegan, `Request-Id` y `Request-Context`: el contexto de
traza de Application Insights con correlacion **Legacy** (`httpCorrelationProtocol`
del diagnostico de APIM). Asi la peticion al modelo comparte `operation_Id` con la
peticion A2A y aparece dentro de la misma transaccion de extremo a extremo. Si APIM
pasa a correlacion W3C, habra que reenviar `traceparent` y `tracestate`. Estas
cabeceras no se registran en el log `request_headers`.
`MAX_RETRIES=0` es el nuevo valor predeterminado, pero revisa los overrides y
ficheros `.env` existentes: un `MAX_RETRIES=2` explicito mantiene los reintentos.

## Dashboards de consumo y acceso en Grafana

El [dashboard unificado de Grafana](dashboards/grafana/README.md) permite consultar
consumo de tokens desde `customMetrics` y peticiones A2A desde `requests` de
Application Insights. Clasifica los HTTP429 por `API Type`: `a2a` como
`request-rate-limit` de agentes y `http` como `llm-rate-limit` del modelo,
segun la convencion de este entorno. Incluye porcentajes independientes por
capa, comparativa por producto/subscription, consumo por modelo y reparto por agente,
departamento, equipo y usuario. Expone la cobertura y las limitaciones de
atribucion de identidad; el filtro de modelo solo afecta a tokens y el acceso
HTTP al modelo solo filtra servicio/producto/subscription. No calcula costes
ni cuota restante, ni confirma la policy real que origina un HTTP429/HTTP403.
La seccion de errores combina rechazos HTTP por capa con excepciones finales
correlacionadas, incluidos fallos de tareas dentro de HTTP200. Las invocaciones
afectadas se deduplican por servicio e `invocation_id`; no se suman a ellas
los intentos HTTP del modelo.

## 3. Clientes Python A2A streaming

Los clientes cargan `.env` y respetan las variables exportadas. Cada cliente
requiere su URL completa de Agent Card en `RISK_AGENT_CARD_URL` o
`CUSTOMER_AGENT_CARD_URL`, por ejemplo
`http://localhost:8001/.well-known/agent-card.json` o
`https://<apim>/risk-discovery`. Si no esta definida o esta vacia, el cliente
termina con un error antes de enviar solicitudes. Si falla la descarga, informa
del error sin intentar otra ruta. No se admiten `--url` ni las antiguas
variables `RISK_AGENT_URL` / `CUSTOMER_AGENT_URL`.
Las llamadas A2A usan la URL JSON-RPC anunciada en `supportedInterfaces` de la
card, no su URL de descarga. Con APIM, la card expuesta por el gateway debe
anunciar el runtime de APIM.

### Solicitudes individuales

```bash
uv run python -m clients.risk \
  --prompt "Analiza los riesgos y los mitigantes de esta empresa ficticia" \
  --document ./informe.txt

uv run python -m clients.customer \
  --prompt "He perdido mi tarjeta, que pasos debo seguir?"
```

El cliente muestra fragmentos de texto mientras llegan. Riesgos admite multiples
`--document` de texto UTF-8 (`.txt` / `.md`), incorporados al prompt; **no hay
extraccion de PDF, RAG ni carga remota de documentos**.

### Carga aleatoria reproducible

**Estas pruebas hacen llamadas reales y generan consumo facturable en Foundry.**
No se ejecuta carga al iniciar los servicios ni los contenedores.

```bash
mkdir -p results

uv run python -m clients.risk --random \
  --requests 3 --concurrency 1 --document-sections 60 --seed 42 \
  --output results/risk.jsonl

uv run python -m clients.customer --random \
  --requests 100 --concurrency 8 --seed 42 \
  --output results/customer.jsonl
```

Sin `--requests` y `--concurrency`, `--random` aplica los valores de la tabla inicial.
Los documentos de Riesgos contienen 60 unidades sinteticas con cifras variables,
escenarios y limitaciones (mas de 30.000 caracteres); las consultas de Atencion
son cortas (menos de 150). No se rellenan prompts con documentos de clientes.

Otros parametros: `--timeout` (240 segundos por solicitud A2A completa),
`--interval` (pausa positiva antes de cada solicitud de cada worker, **no** un
limite global de peticiones/segundo). `--seed` reproduce las entradas, no las
respuestas del modelo. Las opciones deben ser positivas cuando corresponda.
El directorio de `--output` debe existir; cada ejecucion sobrescribe ese archivo.

### Cabeceras por usuario desde CSV

Ambos clientes admiten `--identities-file` con un CSV UTF-8 **sin cabecera**,
con tres columnas y una cuarta opcional:
`department,team,user[,subscription_key]`. Por ejemplo, sin claves:

```csv
finance,globalrisk,david@microsoft.com
retail,cards,michael@microsoft.com
```

El fichero [clients/data/identities.csv](./clients/data/identities.csv) contiene
24 usuarios sinteticos: 4 departamentos, 2 equipos por departamento y 3 usuarios
por equipo.

```bash
uv run python -m clients.risk --random --requests 24 \
  --identities-file clients/data/identities.csv --identity-mode sequence

uv run python -m clients.customer --random --requests 100 --concurrency 8 \
  --identities-file clients/data/identities.csv --identity-mode random --seed 42
```

`--identity-mode sequence` (por defecto) recorre las filas en orden y vuelve a
la primera al agotar el fichero. `random` elige una fila completa al azar por
peticion, con reemplazo: puede repetir usuarios. `--seed` reproduce la seleccion,
independientemente de la generacion aleatoria de prompts. El modo de identidad
es independiente de `--random` y tambien funciona con `--prompt`.
Con concurrencia, la asignacion sigue el numero de peticion; el orden de llegada
o finalizacion puede variar.

Cada peticion envia `X-Department`, `X-Team` y `X-User` de la misma fila, sin
modificar el cliente HTTP compartido. Si la fila contiene una cuarta columna
con clave, envia tambien `Ocp-Apim-Subscription-Key` para esa peticion.
La descarga inicial de la Agent Card utiliza la clave de la identidad
seleccionada para la primera peticion; las peticiones posteriores usan cada
una la clave de su fila. Los productos deben permitir tanto discovery como
runtime cuando ambos requieran subscription key. El agente propaga la misma clave
al endpoint configurado del modelo, junto con las cabeceras `X-*` de identidad
indicadas anteriormente; no reenvia `X-Product-Id` ni `X-Subscription-Id`.
Para usar la clave tambien en el modelo, incluye su API en el mismo producto
y configura su URL de APIM como destino.
La clave no se incluye deliberadamente en logs ni resultados JSONL.
**El cliente no envia `X-Agent`**: APIM
debe introducirlo al llamar al agente. Sin `--identities-file` no se envian
estas cabeceras de identidad ni la subscription key.
El fichero debe contener al menos una fila; los tres primeros campos vacios,
filas con un numero de columnas distinto de tres o cuatro y valores no ASCII imprimibles se rechazan
antes de enviar peticiones. Se eliminan espacios al principio y final de cada
campo. Una cuarta columna vacia equivale a no enviar clave; las claves
admiten como maximo 256 caracteres.
Los errores de lectura o formato terminan el proceso con codigo 1.

Para crear usuarios del Developer Portal de APIM y subscriptions aleatorias
asociadas a productos existentes, usa
[onboard_apim_users.py](deployment/onboard_apim_users.py). Las
[instrucciones de onboarding](deployment/README.md#usuarios-y-subscriptions-de-apim)
explican como guardar la clave primaria al final de cada fila en un fichero
nuevo, anadiendo `.subs` al nombre de entrada. Por ejemplo,
`clients/data/identities.csv` produce `clients/data/identities.csv.subs`;
el original se conserva intacto. Pasa el fichero `.subs` al cliente mediante
`--identities-file`. Los ficheros `*.csv.subs` estan ignorados por Git porque
contienen secretos.

En todas las modalidades se imprime un bloque **Peticion** con el mensaje
completo (incluidos los documentos) **antes de enviarlo**, y despues un bloque
**Respuesta** con el texto recibido. Ambos incluyen agente e identificador.
En solicitudes individuales la respuesta se muestra en streaming; en carga
aleatoria o multiples solicitudes se imprime completa al recibirla, sin mezclar
fragmentos de respuestas concurrentes. Las solicitudes siguen usando A2A streaming.
Si falla una peticion, se identifica la respuesta parcial y se imprime el error.
La consola muestra texto legible, no registros JSON por peticion.

El archivo JSONL de `--output` contiene `request_id`, `agent`, `input`
(prompt completo, incluidos documentos), `output` (respuesta), estado, caracteres
de entrada/salida, latencia total y tiempo hasta el primer fragmento.
Los caracteres **no son tokens**. Si una peticion falla despues de recibir texto,
`output` conserva los fragmentos recibidos, con `status=failed` y el detalle en
`error`; no debe interpretarse como una respuesta completa.
Si el agente proporciona metadata de error A2A, se conserva en `error_details`,
incluidos origen, codigo upstream e identificadores de correlacion. Los errores
de transporte sin metadata dejan ese campo a `null`.
**Estos registros ahora contienen el contenido enviado y recibido**: evita datos
sensibles y protege los archivos y logs de consola. El resumen va a stderr.
Los errores de transporte, tareas fallidas, rechazos y streams incompletos se
notifican; si falla alguna solicitud, el proceso termina con codigo 1.

Los servidores registran eventos JSON `model_usage` con agente, departamento,
despliegue, task ID y los contadores de tokens devueltos por Foundry,
incluido cache/razonamiento si el proveedor los devuelve. No se registran
deliberadamente prompts ni respuestas en esos eventos. Son contadores del modelo,
no una factura; no incluyen necesariamente reintentos o solicitudes fallidas.

El departamento del perfil es contexto ficticio y estatico del agente, no el
departamento del usuario autenticado. No se incluye un campo `team`. Para las
metricas de APIM, la configuracion prevista fija `agent` en la politica de la API
y obtiene `department` y `user` del token validado de cada peticion. Esa politica
no esta implementada en este repositorio. Las cabeceras del CSV permiten simular
estas dimensiones, pero no autentican usuarios: en produccion APIM debe
validarlas o sobrescribirlas con el contexto autenticado. Los perfiles y logs
locales no inicializan dimensiones de APIM.

## 4. Docker: una imagen por agente

Construye **desde la raiz** del proyecto:

```bash
docker build -f docker/risk.Dockerfile -t risk-agent .
docker build -f docker/customer.Dockerfile -t customer-agent .
```

Las imagenes usan Python 3.12, dependencias fijadas en `uv.lock`, usuario no root y
healthcheck. No contienen `.env`, claves ni credenciales.

Ejemplo **en un host de Azure con identidad administrada disponible**:

```bash
docker run --rm --env-file .env -e AUTH_MODE=managed_identity \
  -p 127.0.0.1:8001:8001 risk-agent

docker run --rm --env-file .env -e AUTH_MODE=managed_identity \
  -p 127.0.0.1:8002:8002 customer-agent
```

Un contenedor local no hereda tu `az login` del host, y las imagenes no incluyen
Azure CLI. Para desarrollo local utiliza los procesos Python anteriores.
Para contenedores usa una identidad administrada de Azure o una identidad
federada accesible al contenedor con `AUTH_MODE=default`. Nunca incorpores
credenciales en la imagen. Si usas overrides `RISK_AUTH_MODE` / `CUSTOMER_AUTH_MODE`,
estos prevalecen tambien sobre `AUTH_MODE`.

## 5. GitHub Actions y GitHub Packages (GHCR)

El workflow `.github/workflows/images.yml`:

1. Instala el lockfile, ejecuta Ruff y los tests sin credenciales de Azure.
2. Construye ambas imagenes en una matriz.
3. Publica con `GITHUB_TOKEN` y permiso `packages: write` en:
   - `ghcr.io/<owner>/<repo>-risk`
   - `ghcr.io/<owner>/<repo>-customer`

Se activa con pushes a `main` / `master`, tags `v*` y ejecucion manual.
En pull requests valida y construye, **pero no publica**. Genera tags por SHA,
por tag Git y `latest` en la rama por defecto. No necesita secretos de Foundry
para construir ni publicar.

Para activarlo debes subir el proyecto a un repositorio GitHub y permitir
publicacion de paquetes con `GITHUB_TOKEN` segun la politica de tu organizacion.
La visibilidad del paquete se gestiona en GitHub Packages. Este proyecto no crea
un repositorio remoto ni publica imagenes automaticamente desde tu maquina.

## 6. Despliegue en Azure Container Apps

El directorio [deployment](./deployment/README.md) incluye un script para crear
un entorno y dos Container Apps desde imagenes publicas de GHCR. Configura una
identidad administrada de sistema por app y le asigna **Foundry User** sobre la
cuenta Foundry existente para consumir los modelos sin claves.

El despliegue usa **ingress HTTPS publico sin autenticacion de clientes**;
consulta los requisitos, riesgos y comandos en la guia antes de ejecutarlo.

## 7. Pruebas y depuracion

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Los tests levantan ambos servidores en puertos libres, usan el cliente A2A real
sobre HTTP/SSE y un modelo local determinista de prueba dentro de Agent Framework.
Comprueban fragmentos antes de completar la respuesta, concurrencia aislada,
metricas, tareas fallidas, streams incompletos, configuracion y perfiles de carga.
No llaman a Azure ni sustituyen una prueba con tus despliegues.

En VS Code, con Python Debugger y Foundry Toolkit instalados, pulsa **F5** y
selecciona el agente. Las tareas usan `.venv/bin/python`, arrancan el servidor
real con debugpy y abren Agent Inspector en el puerto correspondiente.
El soporte de protocolos de Inspector depende de la version de la extension:
para verificar A2A usa los clientes Python incluidos. Si cambias los puertos,
actualiza tambien `.vscode/tasks.json`. Deten las tareas de servidor al terminar
de depurar; desconectar el debugger no garantiza terminar el servidor.

### Referencias

- [OpenAI provider de Agent Framework](https://learn.microsoft.com/agent-framework/integrations/by-component/model-providers/openai)
- [Hosting A2A de Agent Framework](https://learn.microsoft.com/agent-framework/hosting/self-hosting/a2a/server)
- [SDK oficial de A2A](https://github.com/a2aproject/a2a-python)
