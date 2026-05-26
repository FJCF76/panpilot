# PanPilot

**Automatización inteligente para el equipo de soporte de Proactivanet S.A.**

PanPilot actúa como capa de gestión administrativa entre Proactivanet y el equipo de agentes. Cuando llega un ticket, PanPilot evalúa el contexto, decide la acción correcta y actúa — sin interrumpir a ningún agente hasta que realmente se necesite su criterio.

---

## Qué problema resuelve

Los agentes de soporte dedican una parte significativa de su jornada a tareas administrativas repetitivas: clasificar tickets, pedir información faltante, recordar a técnicos sin respuesta, y detectar tickets que llevan horas o días sin movimiento. PanPilot automatiza esa capa para que los agentes se concentren en resolver, no en gestionar.

---

## Cuatro comportamientos de automatización

### 1. Triaje y clasificación automática
Cada ticket nuevo es evaluado por Claude. PanPilot asigna prioridad (P1 / P2 / P3) basada en urgencia y contexto, y actualiza el estado del ticket en el mapa de estado interno.

### 2. Respuesta automática L1
Para problemas con solución documentada, PanPilot redacta y publica una respuesta directamente en el ticket. El umbral de confianza configurable (por defecto: 85 %) asegura que solo se envíen respuestas cuando el modelo está seguro.

### 3. Solicitudes de aclaración
Cuando el ticket no tiene suficiente información para actuar, PanPilot hace una pregunta concreta al cliente — máximo dos por ticket para evitar saturar al usuario.

### 4. Alertas de tickets inactivos
Un detector programado revisa periódicamente tickets sin actividad. Cuando un ticket supera el umbral de inactividad según su prioridad (P1: 4 h, P2: 24 h, P3: 120 h), PanPilot registra una alerta interna y notifica al equipo.

---

## Panel de administración

Accesible en `/admin/` (HTTP Basic Auth). Muestra:

- **Registro de auditoría** — cada decisión que PanPilot tomó, con razonamiento en español, acción ejecutada, y si fue en modo prueba (dry-run).
- **Cola de errores (DLQ)** — eventos que fallaron tras tres reintentos, con botón de reintento individual.
- **Filtros** — por ticket, acción, o rango de fechas.

Todas las anotaciones publicadas enlazan directamente al ticket en Proactivanet.

---

## Stack tecnológico

| Componente | Tecnología |
|------------|------------|
| Servidor web | FastAPI (Python 3.12) |
| Motor de IA | Claude (Anthropic API) |
| Base de datos | SQLite con WAL |
| Planificador | APScheduler 3.x |
| Proceso | systemd |
| Proxy inverso | nginx + TLS (certbot) |
| Gestor de paquetes | uv |

---

## Instalación rápida

### Requisitos previos

- Python 3.12 y `uv` instalados en el servidor
- Cuenta de Anthropic con acceso a la API
- Instancia de Proactivanet con webhook configurado hacia `https://panpilot.owncompute.com/webhook/proactivanet`
- Dominio apuntando al servidor (para TLS con certbot)

### Pasos

```bash
# 1. Clonar el repositorio
git clone git@github.com:FJCF76/panpilot.git
cd panpilot

# 2. Instalar dependencias
uv sync

# 3. Copiar y completar variables de entorno
cp .env.example .env
# Editar .env con las credenciales reales (API keys, contraseñas, etc.)
chmod 600 .env

# 4. Preparar el directorio de datos
mkdir -p data

# 5. Instalar el servicio systemd
sudo cp deploy/panpilot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable panpilot
sudo systemctl start panpilot

# 6. Configurar nginx + TLS
sudo cp deploy/panpilot-nginx.conf /etc/nginx/sites-available/panpilot
sudo ln -s /etc/nginx/sites-available/panpilot /etc/nginx/sites-enabled/
sudo certbot --nginx -d panpilot.owncompute.com
sudo systemctl reload nginx
```

### Variables de entorno requeridas

Ver `.env.example` para la lista completa. Las variables mínimas para arrancar:

```
PROACTIVANET_API_URL=https://tu-instancia.proactivanet.com/api
PROACTIVANET_API_KEY=...
PROACTIVANET_AUTHOR_ID=...   # UUID del técnico PanPilot en Proactivanet
PROACTIVANET_BASE_URL=https://tu-instancia.proactivanet.com
ANTHROPIC_API_KEY=...
ADMIN_PASSWORD=...           # contraseña para /admin
DRY_RUN=true                 # cambiar a false tras validar en modo prueba
```

### Verificar que funciona

```bash
# Estado del servicio
sudo systemctl status panpilot

# Últimas entradas del log
journalctl -u panpilot.service -n 50

# Probar el webhook manualmente
curl -s -X POST https://panpilot.owncompute.com/webhook/proactivanet \
  -H "Content-Type: application/json" \
  -d '{"IncidentId": "test-001", "EventType": "Creación"}'
```

---

## Modo prueba (DRY_RUN)

Con `DRY_RUN=true` (el valor por defecto), PanPilot evalúa todos los tickets y registra cada decisión en el log de auditoría, pero **no realiza ninguna llamada de escritura a Proactivanet**. Esto permite validar el comportamiento del sistema durante la Fase 1 antes de activar acciones reales.

Para activar las acciones reales después de la validación:

```bash
# Editar .env
DRY_RUN=false

# Reiniciar el servicio
sudo systemctl restart panpilot
```

---

## Fase 2 — Próximamente

La Fase 2 incorporará **respuestas automáticas basadas en documentación oficial** mediante la Files API de Anthropic (RAG). El sistema accederá al corpus de documentación técnica de Proactivanet para responder preguntas L1 con precisión verificada antes de enviar cualquier respuesta al cliente.

Otros elementos planificados para la Fase 2:

- Alertas automáticas cuando la cola de errores (DLQ) se agota
- Exclusión manual de tickets con campo personalizado de Proactivanet (T13)
- Firma de conformidad de gobernanza de datos (T14)
- Control de recordatorios a nivel de organización (T17)

---

## Documentación técnica

Ver [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) para la referencia completa de arquitectura, modelo de datos, máquina de estados, y decisiones de diseño.
