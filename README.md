# FinOps for AI

## Simulacion de agentes y perfiles de consumo

Dos servicios Python independientes implementados con **Microsoft Agent Framework**.
Los modelos se consumen mediante `FoundryChatClient` (Responses API de un proyecto
Microsoft Foundry). Los servicios no crean agentes remotos ni infraestructura de
Azure; el [script de despliegue](./deployment/README.md) permite aprovisionar
Azure Container Apps de forma explicita.

| Servicio | Departamento ficticio | Despliegue por defecto | Carga sintetica por defecto |
| --- | --- | --- | --- |
| Riesgos | CIB | `gpt-5.6-sol` | 3 solicitudes, concurrencia 1, documentos de 60 secciones |
| Atencion al cliente | Retail | `gpt-5.4-mini` | 100 solicitudes, concurrencia 8, preguntas breves |

Los nombres son **nombres de despliegue**, no una garantia de disponibilidad de esos
modelos en Foundry. Deben existir en tu proyecto; cambia las variables si tienen
otros nombres. El perfil premium/mini simula patrones de consumo, pero el coste real
depende del modelo desplegado, los tokens, la cache y las tarifas de Azure.
No se inventan precios ni se infiere el modelo a partir del nombre del despliegue.

## 1. Configuracion

Requisitos: Python 3.12+, [uv](https://docs.astral.sh/uv/), un proyecto Foundry con
los dos despliegues y una identidad con permisos de inferencia (por ejemplo,
**Foundry User**, anteriormente Azure AI User, en el ambito adecuado).

```bash
uv sync --frozen
cp .env.example .env
```

Edita `.env` con el endpoint de tu proyecto y los nombres de despliegue.
El endpoint debe tener este formato:
`https://<cuenta>.services.ai.azure.com/api/projects/<proyecto>`.
No uses el endpoint de recurso terminado en `/openai/v1`.

La autenticacion es **exclusivamente Azure Identity**, sin API keys:

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
| `FOUNDRY_PROJECT_ENDPOINT` | Endpoint obligatorio compartido |
| `RISK_FOUNDRY_PROJECT_ENDPOINT`, `CUSTOMER_FOUNDRY_PROJECT_ENDPOINT` | Sobrescriben el proyecto por agente |
| `RISK_MODEL_DEPLOYMENT` | `gpt-5.6-sol` |
| `CUSTOMER_MODEL_DEPLOYMENT` | `gpt-5.4-mini` |
| `AUTH_MODE` | `default` en codigo; `cli` en el ejemplo |
| `MANAGED_IDENTITY_CLIENT_ID` | Opcional |
| `API_VERSION` | Opcional; por defecto la del SDK de proyectos fijado en el lockfile |
| `ALLOW_PREVIEW` | `false`; activa APIs preview solo cuando sea necesario |
| `REQUEST_TIMEOUT` | 180 segundos por solicitud HTTP al modelo |
| `MAX_RETRIES` | 2 reintentos del SDK; pueden incrementar llamadas y consumo |
| `LOG_LEVEL` | `INFO` |
| `RISK_HOST`, `CUSTOMER_HOST` | `0.0.0.0` |
| `RISK_PORT`, `CUSTOMER_PORT` | 8001 / 8002 |
| `RISK_PUBLIC_URL`, `CUSTOMER_PUBLIC_URL` | URL anunciada en la Agent Card, sin barra final |
| `RISK_REASONING_EFFORT`, `CUSTOMER_REASONING_EFFORT` | Opcionales; usa solo valores admitidos por tu despliegue |
| `RISK_AGENT_URL`, `CUSTOMER_AGENT_URL` | URL de descubrimiento usada por los clientes |
| `RISK_AGENT_CARD_URL`, `CUSTOMER_AGENT_CARD_URL` | URL completa opcional de la Agent Card; sustituye el descubrimiento por defecto |

`AUTH_MODE`, `MANAGED_IDENTITY_CLIENT_ID`, `API_VERSION`, `ALLOW_PREVIEW`,
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

  Ambos agentes propagan la cabecera HTTP `X-Agent` de cada solicitud A2A a las
  llamadas al modelo mediante `extra_headers` del SDK. El valor se conserva por
  peticion, tambien en streaming y con solicitudes concurrentes; no se modifica
  el cliente compartido ni se introduce en el prompt. Si no llega la cabecera, no
  se envia ni se inventa un valor a partir del perfil. No se propagan otras
  cabeceras entrantes, incluido `Authorization`: se mantiene Azure Identity para
  autenticar las llamadas a Foundry.

  Configura APIM para sobrescribir `X-Agent` con el identificador del agente al
  recibir la llamada A2A. Esta cabecera es metadato, no una prueba de identidad:
  el agente no la autentica y un cliente con acceso directo puede falsificarla.
  APIM solo podra usarla en las metricas del modelo si esas llamadas tambien
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

## 3. Clientes Python A2A streaming

Los clientes cargan `.env` y respetan las variables exportadas. Si se configura
`RISK_AGENT_CARD_URL` o `CUSTOMER_AGENT_CARD_URL`, el cliente correspondiente
descarga la Agent Card directamente desde esa URL completa, por ejemplo
`https://<apim>/customer-support/agent-card.json`. Si no esta definida o esta
vacia, conserva el descubrimiento en
`<RISK_AGENT_URL o CUSTOMER_AGENT_URL>/.well-known/agent-card.json`
(o la URL base indicada mediante `--url`).
Una URL de card configurada tiene prioridad tambien sobre `--url` para el
descubrimiento; si falla, el cliente informa del error sin intentar otra ruta.
Las llamadas A2A usan la URL JSON-RPC anunciada en la card: configura
`RISK_PUBLIC_URL` / `CUSTOMER_PUBLIC_URL` con la direccion que deba alcanzar el
cliente, incluida la ruta de APIM cuando corresponda.

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

Otros parametros: `--url`, `--timeout` (240 segundos por solicitud A2A completa),
`--interval` (pausa positiva antes de cada solicitud de cada worker, **no** un
limite global de peticiones/segundo). `--seed` reproduce las entradas, no las
respuestas del modelo. Las opciones deben ser positivas cuando corresponda.
El directorio de `--output` debe existir; cada ejecucion sobrescribe ese archivo.

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
y la propagacion de identidad no estan implementadas en este repositorio; los
perfiles y logs locales no inicializan dimensiones de APIM.

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

- [Microsoft Foundry provider de Agent Framework](https://learn.microsoft.com/agent-framework/integrations/by-component/model-providers/microsoft-foundry)
- [Hosting A2A de Agent Framework](https://learn.microsoft.com/agent-framework/hosting/self-hosting/a2a/server)
- [SDK oficial de A2A](https://github.com/a2aproject/a2a-python)
