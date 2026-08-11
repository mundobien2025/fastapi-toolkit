# Changelog

## 0.5.4 — 2026-08-11

- TODO: describe changes


## 0.5.3 — 2026-08-01

- TODO: describe changes


Todos los cambios importantes de fastapi-basekit serán documentados aquí.

El formato está basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.0.0/),
y este proyecto adhiere a [Semantic Versioning](https://semver.org/lang/es/).

## [0.5.2] - 2026-07-17

### Corregido

- **`action` resuelve al nombre del endpoint cuando no se llamó `prepare_action`.**
  `BaseController.action` pasó de ser un `ClassVar` (default `None`) a una
  `@property`: usa la acción fijada por `prepare_action` y, si no la hay, cae al
  nombre de la función de endpoint activa (`request.scope["endpoint"].__name__`).
  Antes, un endpoint CUSTOM (create/update/… que no delega en el CRUD base y
  llama `format_response` directo) tenía `action=None`, así que
  `get_schema_class()` devolvía el schema por defecto (de lista) y la validación
  del `response_model` de detalle fallaba con `ResponseValidationError`. Sigue
  sin filtrarse como `?action=` (es property, no campo). Compat: `self.action =
  "x"` sigue permitido vía setter. NO cambia permisos (`prepare_action` sigue
  siendo necesario para `check_permissions`).

## [0.5.1] - 2026-07-17

### Seguridad / hardening

- **Visibilidad ante filtros de scoping descartados (los 3 ORMs).** Cuando un
  filtro no resuelve a ningún campo del modelo (typo, columna/relación
  inexistente, o clave Mongo mal escrita), el repositorio lo descarta — pero
  ahora emite un `logger.warning`. Antes desaparecía en silencio: si esa clave
  venía de `get_filters()` (scoping por tenant/owner), el listado quedaba SIN
  ese filtro → fuga cross-tenant / IDOR invisible. El warning hace la pérdida
  visible en logs/Sentry sin cambiar el resultado de las queries válidas.
  Aplica a `beanie`, `sqlalchemy` y `sqlmodel`. (El passthrough de claves
  Mongo `$or`/`user.$id` en Beanie ya existía desde 0.4.3.)

## [0.5.0] - 2026-07-12

### ⚠️ BREAKING

- **`JWTService` ahora FALLA si falta `JWT_SECRET`.** Antes caía a una clave
  pública por defecto (`secret_dev_key`) y firmaba tokens de producción con un
  secreto conocido por todo el mundo — cualquiera podía forjar tokens de
  cualquier usuario. Ahora `JWTService()` lanza `RuntimeError` si `JWT_SECRET`
  no está seteado. **Migración:** setea `JWT_SECRET` (≥32 bytes aleatorios) en
  todos los entornos. Solo para desarrollo local podés setear
  `JWT_ALLOW_INSECURE_DEV_SECRET=1` para conservar el comportamiento anterior.

### Agregado

- **Tipos completos: `Generic[ModelT]` + `py.typed` (PEP 561)** en repos y
  services de los 3 ORMs (SQLAlchemy, SQLModel, Beanie). Parametrizá
  `class UserRepository(BaseRepository[User])` y el CRUD queda tipado
  (`get`/`create`/`update`/`list`). **Opcional**: subclasear sin el parámetro
  genérico funciona igual (cero cambio en runtime). El marcador `py.typed` hace
  que los tipos inline lleguen a mypy/Pylance/IDE de los consumidores.
- **`Service.post_process_list(items)`** — hook para enriquecer los items de una
  página YA paginada (contador, campo derivado, +1 `await`) sin reescribir la
  paginación ni overridear `list()`. Default no-op.
- **Lectura por id unificada entre ORMs**: `get(id)` **y** `get_by_id(id)`
  funcionan en los 3 repos (aliases) — código portable entre backends.
- **Operadores de filtro en SQLModel** (`__gte`/`__lte`/`__in`/`__ne`/`__like`/
  `__ilike`) — portados desde el repo SQLAlchemy (antes SQLModel los descartaba
  en silencio y devolvía todo).
- **Handler de `DomainError`** en `register_exception_handlers` — un
  `DomainError` que escapa del endpoint ahora se mapea a su `STATUS_CODE_MAP`
  (antes caía al 500 genérico).
- **`fastapi_basekit/py.typed`** empaquetado (PEP 561).
- **Doc `docs/pagination-extension-points.md`** — referencia autoritativa de los
  puntos de extensión de listado/paginación (destilada de proyectos reales).

### Cambiado

- **`BaseController._params` sin introspección de stack-frames.** Reconstruye los
  parámetros leyendo `request.query_params` y coaccionándolos a los tipos
  DECLARADOS en la firma del endpoint (`request.scope["endpoint"]`), de forma
  determinista y **cacheada por endpoint** (~148x menos cómputo que recomputar la
  firma). Arregla listados vacíos/errores intermitentes en controllers cbv
  causados por el `skip_frames` mágico. Se conserva el parámetro `skip_frames`
  (deprecado, ignorado) para back-compat con mixins que overridean `_params`.
- **Coerción de query params por tipo completo**: además de bool/int/float, los
  tipos ricos (`date`, `datetime`, `UUID`, `Decimal`, `Enum`) se coaccionan con
  `pydantic.TypeAdapter` — el mismo motor que FastAPI — para que un filtro por
  fecha/uuid reciba el objeto tipado y no el string.
- **Fix mutable-default por instancia portado a los services SQLAlchemy y
  SQLModel** (antes solo estaba en Beanie): mutar `self.search_fields` ya no
  contamina la clase ni otras requests.
- **Extras usan `psycopg2-binary`** en vez de `psycopg2` (evita build de fuente).

## [0.4.3] - 2026-07-10

### Agregado

- **`BaseRepository.paginate_keyset` (Beanie).** Paginación por cursor (keyset)
  como alternativa O(1)/página a `paginate`: filtra por `cursor_field` y trae
  `limit + 1` para reportar `has_more` sin `count()` ni `skip()`. Para listados
  que escalan (historiales de chat, feeds, tablas append-only) donde la
  paginación offset degrada con la profundidad. Aditivo: `paginate` no cambia.

### Cambiado

- **`BaseService` (Beanie): defaults mutables ahora son por instancia.**
  `search_fields`, `duplicate_check_fields` y `kwargs_query` se copian en
  `__init__` (`list(...)` / `dict(...)`). Mutarlos en runtime ya no se filtra al
  atributo de clase (contaminación cross-request en el mismo proceso). El
  override por subclass se preserva intacto.
- **`BaseRepository.paginate_pipeline` ya no descarta filas en silencio.** Una
  fila que falla `model_validate` se loguea (antes: `except: continue` mudo, que
  devolvía menos ítems que el `limit` sin aviso). Se sigue omitiendo la fila
  corrupta para no romper el listado completo.

## [0.4.2] - 2026-06-18

### Agregado

- **Política de borrado configurable en `BaseService`.** Nuevo `delete_mode` de
  clase con cuatro modos:
  - `"hard"` (default, comportamiento histórico) — elimina físicamente.
  - `"soft"` — marca `deleted_at` (requiere modelo con `soft_delete()`).
  - `"soft_mangle"` — soft + renombra `mangle_fields` (`<valor>__del_<id>`) para
    liberar un valor único y poder recrear el registro (evita choque del unique
    al borrar+recrear el mismo email/cédula/nombre).
  - `"hard_if_unused"` — elimina físicamente si no está referenciado; si lo está
    (según `delete_references = [(Model, "fk_attr"), ...]`) levanta 409.
  - `BaseService.apply_delete(obj)` aplica la política sobre una entidad ya
    cargada (para subclases que scopean el objeto antes de borrar); `delete(id)`
    la usa por defecto.
- **`BaseRepository.save(obj)` y `BaseRepository.hard_delete(obj)`** para
  persistir entidades mutadas y eliminar físicamente sin tocar la sesión desde
  el servicio.

## [0.4.1] - 2026-06-10

### Corregido

- **`action` ahora es filtrable.** Se quitó `"action"` de
  `_params_excluded_fields` (controllers `aio` y `aio/sqlalchemy`). El leak del
  `?action=` espurio ya lo evita el `ClassVar` de `BaseController.action`
  (0.4.0), así que excluirlo del set de filtros era redundante y rompía filtrar
  por una columna `action` declarada como query param (ej. un endpoint de
  audit-logs con `?action=...`). Test de regresión inversa agregado.

## [0.4.0] - 2026-06-09

Mejoras de controller/repository + helpers de cableado reutilizables.

### Agregado

- **`register_exception_handlers(app)`** — cablea el set completo de handlers
  `excepción → BaseResponse` en una sola llamada (antes cada proyecto copiaba
  el mapeo). Suma handler de SQLAlchemy `IntegrityError` y un handler de
  `RequestValidationError` que devuelve el envelope sin relanzar.
- **`simplify_openapi(app)`** — limpia los `operationId`/`summary` generados
  por controllers `@cbv` (quita el prefijo de clase `UserController.`), con
  `summary_overrides` opcional por operación.
- **Filtros con operadores** en `BaseRepository`: sufijo `campo__op` con
  `gte/gt/lte/lt/ne/in/like/ilike`. El operador por defecto (`eq`) conserva la
  semántica `IN` para valores de lista; no rompe los paths de relación
  (`user__role__code`).

### Corregido

- **`action` ya no se filtra como query param espurio** en cada ruta `@cbv`:
  se declara `ClassVar` para que `fastapi-restful` no lo promueva. `prepare_action`
  sigue asignando `self.action` en runtime.
- `_params()` ahora respeta `_params_excluded_fields` (estaba definido pero sin
  usar) y el set incluye `action`.
- `prepare_action` propaga la acción canónica (`list`/`retrieve`/...) a
  `service.action` (antes el service la derivaba del nombre de la función del
  endpoint, p. ej. `list_users`, poco fiable para `get_kwargs_query`).
- `BasePaginationResponse.pagination` ahora tiene default `None` (en Pydantic v2
  era un campo requerido).

## [0.3.5] - 2026-05-20

Release de plugin/skill — sin cambios en el código de la librería
(`pyproject.toml` permanece en 0.3.3).

### Agregado

- **Nueva skill `fastapi-basekit-sentry`** — codifica el patrón de
  observabilidad Sentry/GlitchTip implementado en
  `eventsvileads_backend`. Contempla:
  - `app/utils/observability.py` con `init_sentry`,
    `capture_exception` y `_scrub_event` (before_send hook que
    filtra `authorization`, `cookie`, `x-api-key`, `stripe-signature`
    + body keys `password`, `secret`, `token`, `api_key`,
    `refresh_token`, `access_token`, `credit_card`, `card_number`,
    `cvv`).
  - Captura por excepción desde `app/utils/exception_handlers.py`
    (cero middleware nuevo, cero overhead en responses 2xx) con
    nivel `warning` para 4xx y `error` para 5xx.
  - `init_sentry()` llamado en `app/main.py` ANTES de
    `create_application()` para que las integraciones
    (`Starlette`, `FastAPI`, `Sqlalchemy`, `Redis`) parchen las libs
    antes de que se instancien.
  - Settings (`SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE`,
    `SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`,
    `SENTRY_SEND_PII`) con `send_default_pii=False` por default
    (PII discipline).
  - Pin de dependencia `sentry-sdk[fastapi]>=2.10,<3.0`.
  - GlitchTip-compatible (mismo DSN, mismo formato — self-host gratis).
  - No-op silencioso si SDK no instalado o DSN vacío.
  Incluye checklist de verificación, pitfalls comunes y guía de
  cuándo desviarse del patrón.

## [0.3.4] - 2026-05-18

Release de plugin/skill — sin cambios en el código de la librería
(`pyproject.toml` permanece en 0.3.3).

### Agregado

- **Skill `fastapi-basekit-crud` — regla "Core rule — every function has a
  home (no orphan helpers)"**. Controllers, services y repositories no
  llevan funciones auxiliares sueltas; cada tipo de lógica tiene un hogar
  único: serialización → schema (`BaseSchema` + `@computed_field`),
  factories → `dependency.py`, helpers locales → `app/utils/`, reusables
  → la librería, errores de dominio → un `DomainError` base. Incluye un
  ejemplo trabajado (`_to_response` mal vs bien).
- **Skill `fastapi-basekit-crud` — §30 "List endpoints with filters —
  NEVER reimplement basekit"**. Documenta el flujo completo de `list()`
  (params → `get_filters` → `build_list_queryset` / `build_list_pipeline`
  → paginación), el patrón correcto de controller y service, qué acepta
  el `filters` dict, y los anti-patrones rechazados en review.

## [0.3.3] - 2026-05-02

### Agregado

- **`BaseController.prepare_action(action_name)`**
  (`fastapi_basekit/aio/controller/base.py`).
  Punto único del lifecycle de cada acción: setea `self.action` y corre
  `check_permissions()`. Idempotente por instancia — invocaciones
  repetidas con el mismo `action_name` no re-disparan checks. Llamado
  automáticamente al inicio de cada CRUD estándar (`list`, `retrieve`,
  `create`, `update`, `delete`) en el base + variantes SQLAlchemy /
  SQLModel / Beanie. Métodos custom deben llamarlo manualmente si quieren
  el flujo automático.

- **`permission_classes: List[Type[BasePermission]]`** y
  **`get_permissions()`** estilo DRF en `BaseController`. Override
  `get_permissions()` para despachar permisos por `self.action`:
  ```python
  def get_permissions(self):
      if self.action == "list":
          return [AllowAny]
      return [IsAuthenticated]
  ```

- **`check_permissions_class()`** retro-compat. Alias del nuevo
  `check_permissions()` para controladores pre-0.3.3 que llamaban manual
  el chequeo desde cada endpoint.

### Cambiado

- **`format_response` ahora permisivo**: cuando el `data` (dict / objeto)
  no satisface el `schema_class` del controlador, devuelve el valor crudo
  en vez de levantar `ValidationError`. Útil para endpoints custom que
  retornan dicts ad-hoc sin definir un schema dedicado.

### Tests

- 134 tests passing across the three ORMs (Beanie, SQLAlchemy, SQLModel).
- Suite previously broken (`test_controller_auto_permissions.py` — 17 fails)
  ahora 100% green tras corrección de bugs en los tests:
  - `service = service` shadowing en class body dentro de funciones async
  - `HTTPXAsyncClient(app=app)` deprecated en httpx ≥0.27 → `ASGITransport`
  - Fixtures con `@app.get` sobre clases sin `__call__` → reemplazadas
    por el router canónico `example_crud.controller.router` con `@cbv`
  - Status code esperado en POST: 200 → 201

## [0.3.2] - 2026-05-01

### Agregado (Beanie)

- **`BaseRepository.build_list_queryset` y `build_list_pipeline`**
  (`fastapi_basekit/aio/beanie/repository/base.py`).
  Hooks de extensión equivalentes al `build_list_queryset` de SQLAlchemy
  para componer queries en endpoints `list`. Default delega a
  `build_filter_query` / pipeline básico (`$match` + `$sort`). Subclases
  override para añadir `$lookup`, `$project`, `$group`, etc. — i.e.
  subqueries / JOINs sin tocar controlador ni servicio CRUD.

- **`BaseRepository.paginate_pipeline(pipeline, page, count, validate=True)`**
  Ejecuta un pipeline arbitrario envuelto en `$facet` (paginación + total
  en una sola query). `validate=False` cuando el `$project` produce una
  forma plana distinta del modelo (joined columns).

- **`BaseService.use_aggregation: bool = False`** y
  **`aggregation_validate: bool = True`**.
  Flag de servicio para forzar la ruta de aggregation pipeline (sin
  depender de `order_by` anidado). Cuando `aggregation_validate=False`,
  los dicts crudos del pipeline se devuelven sin validar contra el modelo
  (útil cuando el `$project` aplana subqueries).

- **`BaseService.build_list_queryset` y `build_list_pipeline`**
  (`fastapi_basekit/aio/beanie/service/base.py`). Hooks a nivel servicio
  que delegan en el repositorio. Override en el servicio para añadir
  lookups/proyecciones por endpoint sin contaminar el repo.

### Cambiado (Beanie)

- `BaseService.list` ahora ramifica entre `FindMany` y aggregation
  pipeline según `use_aggregation` o `order_by` anidado. La lógica previa
  se preserva como ruta default; subclases que ya funcionaban no
  necesitan cambios.

- `BaseRepository.list_with_aggregation` se conserva como wrapper
  retrocompatible que delega a `build_list_pipeline` + `paginate_pipeline`.

### Patrón de uso

```python
class AdminUserService(BaseService):
    repository: UserRepository
    use_aggregation = True
    aggregation_validate = False

    def build_list_pipeline(self, search=None, search_fields=None,
                            filters=None, order_by=None, **kwargs):
        pipeline = self.repository.build_list_pipeline(
            search=search, search_fields=search_fields,
            filters=filters, order_by=order_by or "-created_at",
        )
        pipeline.extend([
            {"$lookup": {"from": "wallets", "localField": "_id",
                         "foreignField": "user.$id", "as": "wallet_data"}},
            {"$unwind": {"path": "$wallet_data",
                         "preserveNullAndEmptyArrays": True}},
            {"$project": {
                "id": {"$toString": "$_id"},
                "wallet_balance": {"$convert": {
                    "input": "$wallet_data.balance",
                    "to": "string", "onNull": None,
                }},
                # ... resto del project
            }},
        ])
        return pipeline
```

El controlador solo expone `super().list()` — toda la composición vive
en el pipeline declarativo del servicio.

## [0.3.1] - 2026-04-29

### Agregado

- **`BaseRepository.build_filter_query` — alias `<field>_id` para campos `Link[X]`**
  (`fastapi_basekit/aio/beanie/repository/base.py`).
  Permite filtrar conversaciones/mensajes/cualquier modelo con Link sin
  conocer la sintaxis Mongo nested (`customer.$id`):
  ```python
  # Antes: el caller tenía que escribir filtros raw o
  # subclasear build_filter_query para traducir customer_id → customer.$id.
  # Ahora: basekit auto-traduce.
  service.list(filters={"customer_id": "507f1f77bcf86cd799439011"})
  # → genera Conversation.customer.id == ObjectId(...)
  ```
  Reglas de resolución (en orden):
  1. Key con `.` o `$` → passthrough Mongo (`customer.$id` sigue funcionando).
  2. `hasattr(model, key)` true:
     - Es `Link[X]` → `model.<key>.id == ObjectId(v)`
     - No-Link → `model.<key> == v`
  3. Key termina en `_id` y `model.<base>` es `Link[X]` →
     `model.<base>.id == ObjectId(v)` (alias).
  4. Sin match → ignora silenciosamente (igual que antes).

- **Helper interno `_coerce_objectid()`** — convierte `str`/`ObjectId` a
  `ObjectId` automáticamente cuando la query es contra `Link.id`. Evita que
  el caller tenga que castear manualmente al pasar query params del API.

### Cambiado

- **Mínimos de dependencias**:
  - `pydantic>=2.13.0,<3` (era `>=2.11.7,<3`)
  - `pyjwt>=2.12.1` (era `>=2.10.1`)
  - extras `[beanie]`: `beanie>=2.0,<3` (era `>=1.24.0`); `motor` removido
    porque basekit no lo usa en runtime (sólo en templates cookiecutter).
  - extras `[sqlalchemy]`: `SQLAlchemy[asyncio]>=2.0.30,<3` (era `>=2.0.0`).

### Corregido

- **Import roto**: `from bson import ObjectId, Link` → `from beanie import Link`
  en `aio/beanie/repository/base.py`. `Link` no existe en el paquete `bson`;
  estaba en `beanie.odm.fields.Link` re-exportado en `beanie.__init__`.
  En 0.3.0 el import explotaba al cargar tests aislados aunque la app real
  toleraba el side-effect por orden de carga del intérprete.

## [0.3.0] - 2026-04-26

### Agregado

- **`basekit init` — scaffolder cookiecutter** (`pip install fastapi-basekit[init]`)
  - CLI `basekit` con subcomandos `init` y `version`.
  - Template completo en `fastapi_basekit/templates/project/` con conditionals Jinja.
  - Choices: `orm` (sqlalchemy/beanie), `database` (postgres/mariadb/sqlite/mongodb),
    `server` (uvicorn/gunicorn), `cache` (none/redis), `background_tasks` (none/arq),
    `bucket` (none/s3), `include_alembic`, `include_docker`, `license`.
  - Hooks `pre_gen_project.py` (valida orm↔db) y `post_gen_project.py` (poda archivos no usados).
  - Genera `app/` completo (config, models, repos, services, controllers, middleware, permissions,
    schemas, scripts, utils), `alembic/`, `docker-compose.yml`, `Dockerfile`, `Makefile`,
    `.env.example`, `LICENSE`, `pytest.ini`, `requirements.txt`, `README.md`.
  - Boot end-to-end verificado: `make up-d` → `make migrate-up` → `make seed` → login.

- **Sitio de documentación MkDocs Material** (`pip install fastapi-basekit[docs]`)
  - 22+ páginas: getting-started, user-guide, advanced, api-reference, examples.
  - Hero CSS custom con gradient + grid pattern + cards con hover lift.
  - Mermaid diagrams con tema custom basekit (teal + accent orange).
  - Versionado con `mike`: aliases `latest`, `X.Y`, `dev`.
  - Auto-deploy a GitHub Pages vía workflow.

- **Workflows GitHub Actions reestructurados**
  - `release.yml` — tag `v*` push → PyPI (OIDC trusted publishing) + mike-versioned docs.
    `workflow_dispatch` con inputs `version` / `pypi` / `deploy_docs` para releases parciales.
  - `docs.yml` — push a main con cambios doc-relevantes → deploya alias `dev` (preview).
  - Concurrencia + permisos OIDC + cache pip configurados.

- **Comando único de release** (`scripts/release.py`)
  - Bumpea atómicamente `pyproject.toml`, `plugin.json`, `marketplace.json`, `CHANGELOG.md`.
  - Valida semver + working tree limpio + tag no existe.
  - Commit + tag + push con `--follow-tags`.
  - Modos: `--bump patch|minor|major`, `--dry-run`, `--pypi-only`, `--docs-only`, `--no-changelog`.
  - Makefile targets: `release V=`, `release-no-docs`, `release-docs-only`, `release-dry`.

- **Skill `fastapi-basekit-crud` v2** (`.claude/skills/fastapi-basekit-crud/SKILL.md`)
  - Secciones nuevas: §22 servicios deben extender `BaseService` (incluso no-CRUD),
    §23 alembic `render_item` para `GUID` + `LowercaseEnum`, §24 API real de `JWTService`
    (`create_token`, NO `encode_token`), §25 política de extender `SQLAlchemyBaseController`,
    §26 pinning de deps, §27 checklist sync→async migration, §28 `get_db` lean
    (translation en handlers, no en generator).

- **Documentación interna `RELEASING.md`**
  - Guía completa para mantenedores: setup PyPI OIDC, GitHub Pages bootstrap,
    release flow, modos parciales, mike aliases, troubleshooting (8 escenarios), checklist.

### Corregido

- **`BasePaginationResponse[Schema]`** (no `[List[Schema]]`)
  - `BasePaginationResponse` ya declara `data: List[T]`. Wrappear con `List[]` doble-anida
    a `List[List[T]]` y Pydantic itera filas como tuplas `(field, value)` → 8 errores per row.
  - Fix en swapdealer + skill + template cookiecutter.

- **Mermaid runtime en docs** (Material 9.7+)
  - Material removió auto-bootstrap de mermaid.
  - Carga explícita CDN + init script con tema matching + hook re-render en instant nav.

- **Alembic `render_item` para tipos custom**
  - Sin esto, autogen emite `app.models.types.LowercaseEnum(...)` literal → `NameError` en upgrade.
  - Renderiza `GUID` y `LowercaseEnum` como `sa.String(length=N)`.

### Cambiado

- **`get_db` lean** — solo `yield + commit / except: rollback + raise`.
  Translation de errores (IntegrityError → mensaje friendly) vive en `app/utils/exception_handlers.py`,
  registrado globalmente. Saca domain knowledge del session generator.

- **`self.action` automático** en controllers/services — `BaseController.__init__` lo asigna
  desde `request.scope["endpoint"].__name__`. Skill actualizada: nunca asignar manual,
  branch en `self.action == "method_name"`.

- **mkdocs.yml** — `pymdownx.emoji` con `material.extensions.emoji.twemoji`,
  custom palette (teal/deep-orange), font Inter + JetBrains Mono, navigation features ampliados.

### Empaquetado

- Optional extras nuevos: `[init]` (cookiecutter), `[docs]` (mkdocs-material + mkdocstrings).
- `[project.scripts]` registra `basekit = "fastapi_basekit.cli:main"`.
- `package-data` incluye templates en el wheel.

## [0.2.0] - 2026-02-27

### ✨ Agregado

- **Soporte para SQLModel ORM** (`fastapi_basekit.aio.sqlmodel`)
  - `BaseRepository` — repositorio async con `sqlmodel.ext.asyncio.session.AsyncSession`.
    Usa `session.exec()` para queries tipados sobre modelos SQLModel; mantiene
    `session.execute()` para consultas de agregación y queries complejos con
    Result Hydration. Contrato idéntico al repositorio SQLAlchemy.
  - `BaseService` — servicio base que referencia el repositorio SQLModel.
    Mismas operaciones: `retrieve`, `list`, `create`, `update`, `delete`.
  - `SQLModelBaseController` — controlador base con soporte para `joins`,
    `use_or` y `order_by`. La serialización aprovecha que los modelos SQLModel
    son también modelos Pydantic (`model_dump()` nativo).
  - `to_dict()` simplificado en el controlador gracias a la integración
    Pydantic de SQLModel.

- **Dependencia opcional `sqlmodel`** en `pyproject.toml`
  - Instalar con: `pip install fastapi-basekit[sqlmodel]`
  - Incluido en el extra `[all]`.

## [0.1.25] - 2026-02-11

### ✨ Agregado

- **Vínculo bidireccional Servicio-Repositorio**
  - `BaseRepository` ahora tiene un atributo `service` que referencia al servicio que lo utiliza.
  - `BaseService` vincula automáticamente su instancia (`self`) a todos los repositorios inyectados durante la inicialización.

### 🔧 Cambiado

- **Refactor de Construcción de QuerySet**
  - Se eliminó el método redundante `build_queryset` de `BaseService`.
  - El método `BaseRepository.list_paginated` ahora es el único responsable de iniciar la construcción del query a través de `build_list_queryset()`.
  - Se simplificó la firma de `list_paginated` eliminando el parámetro `base_query`.
  - `BaseService.list` ahora es más limpio al delegar la gestión del query completamente al repositorio.

### 🔧 Mejorado

- **Inicialización de Servicios**
  - `BaseService.__init__` ahora acepta `**kwargs` y vincula automáticamente cualquier instancia de `BaseRepository` pasada como argumento nombrado, permitiendo que servicios complejos con múltiples repositorios mantengan el vínculo correctamente.

## [0.1.16] - 2025-10-14

### ✨ Agregado

- **Controllers completamente separados por ORM/ODM**

  - `BeanieBaseController`: Controller específico para proyectos con MongoDB/Beanie
  - `SQLAlchemyBaseController`: Controller específico para proyectos con SQLAlchemy
  - Cada controller tiene implementación completa y optimizada para su ORM/ODM
  - Ya no hay herencia de un `BaseController` genérico compartido

- **SQLAlchemyBaseController: Nuevas capacidades**

  - Soporte completo para JOINs con `joins` parameter
  - Soporte para expresiones `ORDER BY` personalizadas
  - Operador `OR` en filtros con `use_or=True`
  - Método `to_dict()` mejorado para modelos SQLAlchemy
  - `_params_excluded_fields` incluye automáticamente `use_or`, `joins`, `order_by`

- **BeanieBaseController: Optimizado para MongoDB**

  - Implementación optimizada para documentos Beanie
  - Extracción automática de parámetros sin frames extras
  - Método `to_dict()` específico para documentos MongoDB

- **Documentación completa**
  - Nuevo archivo `CONTROLLERS_GUIDE.md` con guía detallada
  - Ejemplos de uso para cada controller
  - Tabla comparativa de características
  - Guía de migración desde versiones anteriores

### 🔧 Cambiado

- **Dependencias más flexibles**

  - `fastapi`: `>=0.116.1,<0.117` (antes: `==0.116.1`)
  - `pydantic`: `>=2.11.7,<3` (antes: `==2.11.7`)
  - `fastapi-restful[all]`: `>=0.6.0,<0.7` (antes: `==0.6.0`)
  - `SQLAlchemy[asyncio]`: `>=2.0.43,<3` (antes: `==2.0.43`)
  - `psycopg2`: `>=2.9.10,<3` (antes: `==2.9.10`)

- **BaseController.format_response()**
  - Parámetro `status` renombrado a `response_status` para evitar conflictos
  - Mejora la compatibilidad con imports de Starlette/FastAPI

### 🐛 Corregido

- **`_params()` ahora funciona correctamente en SQLAlchemyBaseController**

  - Solucionado problema de introspección de frames
  - Agregado parámetro `skip_frames` para navegar correctamente en la pila
  - SQLAlchemy ahora usa `skip_frames=2` para capturar parámetros correctamente

- **Eliminado conflicto con parámetro `status`**
  - El parámetro `status` en `format_response()` podía generar conflictos
  - Ahora se llama `response_status` para mayor claridad

### 📚 Documentación

- README actualizado con sección de controllers separados
- Guía completa en `CONTROLLERS_GUIDE.md`
- Ejemplos actualizados para ambos controllers
- Tabla comparativa de características

### 🔄 Migración desde v0.1.15

**Antes:**

```python
from fastapi_basekit.aio.controller.base import BaseController
```

**Ahora:**

```python
# Para SQLAlchemy
from fastapi_basekit.aio.sqlalchemy import SQLAlchemyBaseController

# Para Beanie
from fastapi_basekit.aio.beanie import BeanieBaseController
```

El `BaseController` genérico sigue disponible para compatibilidad, pero se recomienda usar los controllers específicos.

---

## [0.1.15] - 2025-10-XX

### Agregado

- Controller base genérico con soporte para Beanie y SQLAlchemy
- Sistema de permisos basado en clases
- Extracción automática de parámetros con `_params()`
- Paginación automática
- Búsqueda multi-campo

### Cambiado

- Mejoras en la estructura del proyecto

---

## [0.1.0] - 2025-XX-XX

### Agregado

- Versión inicial de fastapi-basekit
- Soporte básico para SQLAlchemy y Beanie
- Repositorios base
- Servicios base
- Schemas base

---

[0.3.0]: https://github.com/mundobien2025/fastapi-basekit/compare/v0.2.1...v0.3.0
[0.2.0]: https://github.com/mundobien2025/fastapi-basekit/compare/v0.1.25...v0.2.0
[0.1.25]: https://github.com/mundobien2025/fastapi-basekit/compare/v0.1.16...v0.1.25
[0.1.16]: https://github.com/mundobien2025/fastapi-basekit/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/mundobien2025/fastapi-basekit/compare/v0.1.0...v0.1.15
[0.1.0]: https://github.com/mundobien2025/fastapi-basekit/releases/tag/v0.1.0

