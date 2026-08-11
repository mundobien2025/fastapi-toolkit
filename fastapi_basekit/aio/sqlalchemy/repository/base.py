from typing import (
    Any,
    Dict,
    Generic,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
    TypeVar,
    Union,
)
from uuid import UUID
import logging

from sqlalchemy import Select, and_, or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Relationship, joinedload, selectinload

from ....exceptions.api_exceptions import NotFoundException

logger = logging.getLogger(__name__)

#: Tipo del modelo ORM que gestiona el repositorio. Parametrízalo al declarar
#: la subclase — ``class UserRepository(BaseRepository[User])`` — para que
#: ``get``/``create``/``update``/``list_paginated`` devuelvan ``User`` tipado
#: (autocompletado + mypy para consumidores e IAs).
ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    """
    Repositorio base para SQLAlchemy Async, parametrizado por el modelo.

    Define operaciones CRUD, acceso por filtros y carga de relaciones (joins).
    Declara el modelo en la subclase vía el parámetro genérico Y el atributo
    de clase::

        class UserRepository(BaseRepository[User]):
            model = User

    A partir de ahí ``repo.get(id)`` es ``Optional[User]``, ``repo.create(...)``
    es ``User``, etc.
    """

    model: Type[ModelT]
    service: Optional[Any] = None

    def __init__(self, db: AsyncSession):
        """Inicializa el repositorio con la sesión a reutilizar."""
        self._session = db
        self.service = None

    @property
    def session(self) -> AsyncSession:
        return self._session

    def _get_field(self, field_name: str):
        """Valida y retorna el atributo (columna) del modelo."""
        if not self.model:
            raise ValueError("El modelo no está definido en el repositorio")
        field = getattr(self.model, field_name, None)
        if not field:
            raise AttributeError(
                f"El campo '{field_name}' no existe en {self.model.__name__}"
            )
        return field

    def _apply_joins(
        self, query: Select[Tuple[Any]], joins: Optional[List[str]]
    ) -> Any:
        """Aplica las opciones de carga (joins) dinámicamente al query."""
        if joins:
            for relation in joins:
                if hasattr(self.model, relation):
                    relationship_attr = getattr(self.model, relation)
                    if getattr(relationship_attr.property, "uselist", False):
                        query = query.options(selectinload(relationship_attr))
                    else:
                        query = query.options(joinedload(relationship_attr))
        return query

    def _resolve_field_path(
        self, path: str
    ) -> Tuple[Optional[Any], Dict[str, Any]]:
        """
        Resuelve una ruta de campo (ej. 'user__role__name') a su atributo
        de SQLAlchemy y el conjunto de JOINs necesarios para el filtrado.

        Args:
            path: Ruta del campo con sintaxis '__'

        Returns:
            Tuple con:
            - attr: Atributo final (columna o relación) o None si no existe
            - joins_to_apply: Dict con las relaciones intermedias (name: attr)
        """
        parts = path.split("__")
        current_model = self.model
        attr = getattr(current_model, parts[0], None)
        joins_to_apply = {}

        if attr is None:
            return None, {}

        # Recorrer la cadena de relaciones intermedias
        for part in parts[1:]:
            # Chequear si el atributo actual es una relación ORM
            if hasattr(attr.property, "entity") and isinstance(
                attr.property, Relationship
            ):
                # Es una relación: Añadir al diccionario de JOINs
                relation_name = attr.key
                joins_to_apply[relation_name] = attr

                # Mover al modelo relacionado y obtener el siguiente atributo
                current_model = attr.property.mapper.class_
                attr = getattr(current_model, part, None)

                if attr is None:
                    return None, {}
            else:
                # No es una relación o es el final del path
                break

        return attr, joins_to_apply

    def _resolve_attribute(
        self, filters: Dict[str, Any]
    ) -> Tuple[Dict[str, Tuple[Any, Any]], Dict[str, Any]]:
        """
        Resuelve los atributos finales y las relaciones para JOINs a partir
        de una ruta con '__'.

        Soporta filtrado avanzado con relaciones usando sintaxis de doble
        guion bajo (__). Por ejemplo:
        - Filtro simple: {"status": "active"} → WHERE users.status = 'active'
        - Filtro con relación: {"user_roles__role__code": "Impulsado"} →
          JOINs automáticos + condición WHERE

        Args:
            filters: Diccionario con filtros que pueden incluir rutas con '__'

        Returns:
            Tuple con:
            - resolved_filters: Dict con (columna/atributo final, valor
              procesado)
            - joins_to_apply: Dict con las relaciones que necesitan JOINs
              (nombre_relacion: atributo_relacion)

        Note:
            Los filtros que no se pueden resolver (campos/relaciones
            inexistentes) se omiten silenciosamente. Si todos los filtros
            son omitidos, `resolved_filters` estará vacío y los métodos
            que usan este resultado deben manejar este caso (retornando
            lista vacía o None según corresponda).
        """
        resolved_filters = {}
        joins_to_apply = {}

        for filter_path, value in filters.items():
            # Detect an optional trailing operator suffix (e.g.
            # "age__gte", "created_at__lte", "name__ilike", "id__in").
            # The remaining path is resolved normally, so relationship
            # traversal ("user__role__code") keeps working — only a final
            # token that matches a known operator is treated as one.
            resolve_path, op = self._split_operator(filter_path)
            attr, field_joins = self._resolve_field_path(resolve_path)

            if attr is None:
                # Filtro no resoluble (typo, columna/relación inexistente). Se
                # descarta — pero se AVISA: si venía de `get_filters` (scoping
                # por tenant/owner), su desaparición silenciosa deja el listado
                # sin filtrar → fuga cross-tenant (IDOR). El warning hace
                # visible la pérdida en logs en vez de fallar en silencio.
                logger.warning(
                    "filtro '%s' descartado (no resuelve a ningún campo de %s). "
                    "Si era un filtro de scoping, el listado queda SIN ese filtro.",
                    filter_path,
                    getattr(self.model, "__name__", self.model),
                )
                continue

            joins_to_apply.update(field_joins)

            # Procesar valor (manejo de Enum a su valor/lista de valores)
            processed_value = value
            if (
                isinstance(value, (list, tuple))
                and value
                and hasattr(value[0], "value")
            ):
                processed_value = [v.value for v in value]
            elif hasattr(value, "value"):
                processed_value = value.value

            # Verificar que no sea una relación (solo queremos columnas para filtrar)
            is_relationship = isinstance(attr.property, Relationship)
            is_column = hasattr(attr.property, "columns") or hasattr(
                attr, "comparator"
            )

            if not is_relationship and is_column:
                resolved_filters[filter_path] = (attr, processed_value, op)

        return resolved_filters, joins_to_apply

    # Operadores de filtro soportados como sufijo "campo__op".
    FILTER_OPERATORS = frozenset(
        {"eq", "ne", "gt", "gte", "lt", "lte", "in", "like", "ilike"}
    )

    def _split_operator(self, filter_path: str) -> Tuple[str, str]:
        """Separa un sufijo de operador del path de filtro.

        "age__gte" → ("age", "gte"); "user__role__code" → (..., "eq").
        Solo se interpreta como operador si el último token coincide con
        ``FILTER_OPERATORS`` y queda al menos un token de campo delante.
        """
        if "__" in filter_path:
            head, _, last = filter_path.rpartition("__")
            if head and last in self.FILTER_OPERATORS:
                return head, last
        return filter_path, "eq"

    @staticmethod
    def _condition_for(field: Any, value: Any, op: str) -> Any:
        """Construye la condición SQLAlchemy para un operador dado."""
        if op == "ne":
            return field != value
        if op == "gt":
            return field > value
        if op == "gte":
            return field >= value
        if op == "lt":
            return field < value
        if op == "lte":
            return field <= value
        if op == "in":
            seq = value if isinstance(value, (list, tuple, set)) else [value]
            return field.in_(list(seq))
        if op == "like":
            return field.like(f"%{value}%")
        if op == "ilike":
            return field.ilike(f"%{value}%")
        # op == "eq" (default): conserva la semántica IN para listas.
        if isinstance(value, (list, tuple)) and not isinstance(value, str):
            return field.in_(value)
        return field == value

    def _resolve_order_by(
        self,
        order_by: Optional[Any] = None,
        special_fields: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Any], Dict[str, Any]]:
        """
        Resuelve ordenamiento y relaciones para JOINs.

        Método general que resuelve rutas de relaciones usando
        sintaxis '__' y soporta prefijo '-' para orden descendente.
        Similar a _resolve_attribute pero adaptado para order_by.

        Soporta ordenamiento avanzado con relaciones usando sintaxis
        de doble guion bajo (__). Por ejemplo:
        - Orden simple: "created_at" → ORDER BY model.created_at ASC
        - Orden descendente: "-created_at" → ORDER BY model.created_at DESC
        - Orden con relación: "user__full_name" →
          JOIN users + ORDER BY users.full_name ASC
        - Orden descendente con relación: "-user__email" →
          JOIN users + ORDER BY users.email DESC
        - Campos especiales (aliases): "borrower_name" →
          ORDER BY Users.full_name ASC (si está en special_fields)

        Args:
            order_by: Campo por el cual ordenar. Puede incluir:
                     - Campos del modelo: "created_at", "status", etc.
                     - Prefijo '-' para descendente: "-created_at", "-status"
                     - Rutas de relaciones: "user__full_name",
                       "user__role__name"
                     - Combinaciones: "-user__email"
                     - Campos especiales: "borrower_name" (si está en
                       special_fields)
                     - También puede ser una expresión SQLAlchemy
            default_order_by: Campo por defecto si order_by es None o
                             no válido.
                             Por defecto: "created_at"
            special_fields: Diccionario opcional que mapea nombres de campos
                          especiales (aliases en el query) a atributos
                          SQLAlchemy. Ejemplo:
                          {"borrower_name": Users.full_name,
                           "borrower_email": Users.email}
                          Estos campos ya están disponibles en el query sin
                          necesidad de JOIN adicional.

        Returns:
            Tuple con:
            - order_expression: Expresión SQLAlchemy para order_by
              (None si no se pudo resolver, o el mismo order_by si ya
              es una expresión SQLAlchemy)
            - joins_to_apply: Dict con las relaciones que necesitan JOINs
              (nombre_relacion: atributo_relacion)

        Note:
            Si el campo no se puede resolver (campo/relación inexistente),
            retorna (None, {}). Los métodos que usan este resultado deben
            manejar este caso aplicando un ordenamiento por defecto.
            Si order_by ya es una expresión SQLAlchemy (no string), la retorna
            sin modificar.
        """
        # Si order_by ya es una expresión SQLAlchemy (no string), retornarla
        # directamente sin procesar
        if order_by is not None and not isinstance(order_by, str):
            return order_by, {}

        if not order_by:
             # Si no hay order_by, no ordenamos (o el caller debería haber pasado un default)
             return None, {}

        joins_to_apply: Dict[str, Any] = {}

        # Detectar si tiene prefijo '-' para invertir el orden
        has_minus_prefix = order_by.startswith("-")
        field_path = order_by.lstrip("-")

        # 1. Verificar primero si es un campo especial (alias en el query)
        # Estos campos ya están disponibles sin necesidad de JOIN
        if special_fields and field_path in special_fields:
            attr = special_fields[field_path]
            # Determinar dirección del ordenamiento
            should_descend = has_minus_prefix
            order_expression = attr.desc() if should_descend else attr.asc()
            return order_expression, {}

        # 2. Si no es un campo especial, procesar con el helper
        attr, field_joins = self._resolve_field_path(field_path)

        if attr is None:
            return None, {}

        joins_to_apply.update(field_joins)

        # 4. Verificar que el atributo final es válido para ordenar
        is_relationship = isinstance(attr.property, Relationship)
        is_column = hasattr(attr.property, "columns") or hasattr(
            attr, "comparator"
        )

        if is_relationship or not is_column:
            return None, {}

        # 5. Determinar la dirección del ordenamiento
        should_descend = has_minus_prefix

        # 6. Crear la expresión de ordenamiento
        order_expression = attr.desc() if should_descend else attr.asc()

        return order_expression, joins_to_apply

    def _build_conditions(
        self,
        filters: Optional[Dict[str, Any]] = None,
        resolved_filters: Optional[Dict[str, Tuple[Any, Any]]] = None,
    ) -> List[Any]:
        """
        Construye condiciones WHERE a partir de los atributos resueltos.

        Soporta dos modos de uso:
        1. Modo legacy (retrocompatibilidad): Pasa filters directamente
        2. Modo avanzado: Pasa resolved_filters con atributos ya resueltos

        Args:
            filters: Diccionario de filtros simples (modo legacy)
            resolved_filters: Diccionario con (atributo, valor_procesado)
                             (modo avanzado)

        Returns:
            Lista de condiciones SQLAlchemy
        """
        conditions: List[Any] = []

        # Modo avanzado: usar resolved_filters si está disponible
        if resolved_filters is not None:
            for _, resolved in resolved_filters.items():
                # Tupla (field, value) legacy o (field, value, op) con operador.
                field, processed_value = resolved[0], resolved[1]
                op = resolved[2] if len(resolved) > 2 else "eq"
                conditions.append(
                    self._condition_for(field, processed_value, op)
                )
        # Modo legacy: compatibilidad con código existente
        elif filters is not None:
            for fn, value in filters.items():
                field = self._get_field(fn)
                if isinstance(value, (list, tuple)) and not isinstance(
                    value, str
                ):
                    conditions.append(field.in_(value))
                else:
                    conditions.append(field == value)

        return conditions

    def _build_search_condition(
        self, search: Optional[str], search_fields: Optional[List[str]]
    ) -> Tuple[Optional[Any], Dict[str, Any]]:
        """Construye una condición OR para búsqueda textual en múltiples campos

        Usa ilike (insensible a mayúsculas) si hay término y campos válidos.
        Soporta rutas anidadas con '__'.

        Returns:
            Tuple con:
            - search_condition: Condición OR de SQLAlchemy o None
            - search_joins: Dict con JOINs necesarios para la búsqueda
        """
        if not search or not search_fields:
            return None, {}
        
        exprs: List[Any] = []
        search_joins: Dict[str, Any] = {}

        for field_path in search_fields:
            attr, field_joins = self._resolve_field_path(field_path)
            
            if attr is None:
                continue

            search_joins.update(field_joins)

            # Verificar que sea una columna válida para ILIKE
            is_column = hasattr(attr.property, "columns") or hasattr(
                attr, "comparator"
            )
            is_relationship = isinstance(attr.property, Relationship)

            if not is_relationship and is_column:
                exprs.append(attr.ilike(f"%{search}%"))

        if not exprs:
            return None, {}

        return or_(*exprs), search_joins

    async def create(self, obj_in: Union[ModelT, Dict[str, Any]]) -> ModelT:
        """Crea un nuevo registro en la base de datos."""
        db = self.session
        if isinstance(obj_in, dict):
            obj_in = self.model(**obj_in)
        db.add(obj_in)
        await db.flush()
        await db.refresh(obj_in)
        return obj_in

    async def _get_one(
        self,
        conditions: Optional[List[Any]] = None,
        joins: Optional[List[str]] = None,
        raise_exception: bool = False,
    ) -> Optional[Any]:
        """
        Método interno unificado para obtener un único registro.

        Args:
            conditions: Lista de condiciones WHERE
            joins: Lista de relaciones para carga eager
            raise_exception: Si True, lanza excepción si no se encuentra

        Returns:
            Registro encontrado o None
        """
        if not self.model:
            raise ValueError("El modelo no está definido en el repositorio")

        query = select(self.model)
        if conditions:
            query = query.where(and_(*conditions))

        query = self._apply_joins(query, joins)

        result = await self.session.execute(query)
        record = result.scalars().first()

        if raise_exception and record is None:
            raise NotFoundException(
                message=f"{self.model.__name__} no encontrado"
            )

        return record

    async def get(
        self,
        record_id: Union[str, UUID],
        filters: Optional[Dict[str, Any]] = None,
    ) -> Optional[ModelT]:
        """Obtiene un registro por su ID.

        `filters` (opcional) — scoping de seguridad (típicamente
        `Service.get_filters()`). Sin `filters`, usa `session.get()` (fast
        path por PK) — comportamiento idéntico a antes, 100% retrocompatible.
        Con `filters`, cae a un `SELECT ... WHERE id = :id AND <filtros>` —
        un id que existe pero no matchea el scope devuelve `None` (mismo
        resultado que "no encontrado"), cerrando la clase de IDOR de "conozco
        el id de otro tenant y lo pido directo"."""
        if not self.model:
            raise ValueError("El modelo no está definido en el repositorio")
        if not filters:
            return await self.session.get(self.model, record_id)
        conditions = [self.model.id == record_id] + self._build_conditions(
            filters=filters
        )
        return await self._get_one(conditions=conditions)

    async def get_by_id(
        self,
        record_id: Union[str, UUID],
        filters: Optional[Dict[str, Any]] = None,
    ) -> Optional[ModelT]:
        """Alias de `get(id)` — nombre unificado con el repo Beanie
        (`get_by_id`) para que las lecturas por id sean portables entre ORMs."""
        return await self.get(record_id, filters=filters)

    async def get_by_field(
        self, field_name: str, value: Any
    ) -> Optional[ModelT]:
        """Obtiene un registro por un campo específico."""
        field = self._get_field(field_name)
        return await self._get_one(conditions=[field == value])

    async def get_with_joins(
        self,
        record_id: Union[str, UUID],
        joins: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Optional[ModelT]:
        """Obtiene un registro por ID y carga relaciones dinámicamente.

        `filters` (opcional) — mismo scoping de seguridad que `get()`."""
        conditions = [self.model.id == record_id]
        if filters:
            conditions.extend(self._build_conditions(filters=filters))
        return await self._get_one(conditions=conditions, joins=joins)

    async def get_by_field_with_joins(
        self,
        field_name: str,
        value: Any,
        joins: Optional[List[str]] = None,
    ) -> Optional[Any]:
        """
        Obtiene un registro por un campo y carga relaciones dinámicamente.

        Soporta filtros con relaciones usando sintaxis '__'.
        """
        # Si el campo tiene sintaxis '__', usar resolución de atributos
        if "__" in field_name:
            # Resolver el atributo y aplicar JOINs necesarios
            resolved_filters, joins_to_apply = self._resolve_attribute(
                {field_name: value}
            )

            if not resolved_filters:
                return None

            # Obtener el atributo resuelto (puede traer operador como 3er elem)
            _, resolved = next(iter(resolved_filters.items()))
            field, processed_value = resolved[0], resolved[1]
            op = resolved[2] if len(resolved) > 2 else "eq"

            # Construir query con JOINs de filtrado
            query = select(self.model)

            # Aplicar JOINs de filtrado
            for relation_name, relation_attr in joins_to_apply.items():
                query = query.join(relation_attr)

            # Aplicar condición
            query = query.where(self._condition_for(field, processed_value, op))

            # Aplicar joins de carga
            query = self._apply_joins(query, joins)

            result = await self.session.execute(query)
            return result.scalars().first()
        else:
            # Campo simple, usar método estándar
            field = self._get_field(field_name)
            return await self._get_one(
                conditions=[field == value], joins=joins
            )

    async def get_by_filters(
        self, filters: Dict[str, Any], use_or: bool = False
    ) -> Sequence[Any]:
        """
        Obtiene registros que coinciden con los filtros especificados.

        Soporta filtros simples y filtros con relaciones usando sintaxis '__'.
        """
        if not self.model:
            raise ValueError("El modelo no está definido en el repositorio")

        # Resolver filtros y JOINs necesarios
        resolved_filters, joins_to_apply = self._resolve_attribute(filters)

        # Inicializar query
        query = select(self.model)

        # Aplicar JOINs de filtrado si hay relaciones
        for relation_name, relation_attr in joins_to_apply.items():
            query = query.join(relation_attr)

        # Construir condiciones
        conditions = self._build_conditions(resolved_filters=resolved_filters)

        # Verificar si hay condiciones antes de combinarlas
        # and_() y or_() requieren al menos un argumento
        if not conditions:
            # Si no hay condiciones válidas después de resolver filtros,
            # retornar lista vacía (comportamiento restrictivo)
            return []

        combined = or_(*conditions) if use_or else and_(*conditions)
        query = query.where(combined)

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_by_filters_with_joins(
        self,
        filters: Dict[str, Any],
        use_or: bool = False,
        joins: Optional[List[str]] = None,
        one: bool = False,
    ) -> Sequence[Any] | Optional[Any]:
        """
        Obtiene registros por filtros y carga relaciones dinámicamente.

        Soporta filtros simples y filtros con relaciones usando sintaxis '__'.
        Los JOINs de filtrado se aplican automáticamente cuando se detectan
        relaciones en los filtros.
        """
        # Resolver filtros y JOINs necesarios
        resolved_filters, joins_to_apply = self._resolve_attribute(filters)

        # Inicializar query
        query = select(self.model)

        # Aplicar JOINs de filtrado
        for relation_name, relation_attr in joins_to_apply.items():
            query = query.join(relation_attr)

        # Construir condiciones
        conditions = self._build_conditions(resolved_filters=resolved_filters)

        # Verificar si hay condiciones antes de combinarlas
        # and_() y or_() requieren al menos un argumento
        if not conditions:
            # Retornar None si one=True, o lista vacía si one=False
            return None if one else []

        combined = or_(*conditions) if use_or else and_(*conditions)
        query = query.where(combined)

        # Aplicar joins de carga (joinedload/selectinload)
        query = self._apply_joins(query, joins)

        result = await self.session.execute(query)
        return result.scalars().first() if one else result.scalars().all()

    def apply_list_filters(
        self,
        queryset: Select[Tuple[Any]],
        filters: Optional[Dict[str, Any]] = None,
        use_or: bool = False,
        joins: Optional[List[str]] = None,
        order_by: Optional[Any] = None,
        search: Optional[str] = None,
        search_fields: Optional[List[str]] = None,
    ) -> Select[Tuple[Any]]:
        """Aplica filtros estándar, búsqueda y ordenamiento al query."""
        filters = filters or {}
        
        # 1. RESOLUCIÓN UNIVERSAL DE FILTROS (JOINs de filtrado y atributos)
        resolved_filters, joins_to_apply = self._resolve_attribute(filters)

        # 2. Aplicar JOINs de filtrado
        for relation_name, relation_attr in joins_to_apply.items():
            queryset = queryset.join(relation_attr)

        # 3. Construir las condiciones WHERE
        conditions = self._build_conditions(resolved_filters=resolved_filters)
        combined_filters = (
            or_(*conditions)
            if (use_or and conditions)
            else and_(*conditions) if conditions else None
        )

        # 4. Aplicar búsqueda textual (search)
        search_condition, search_joins = self._build_search_condition(
            search, search_fields
        )

        # 5. Aplicar JOINs de búsqueda
        for relation_name, relation_attr in search_joins.items():
            # Evitar unir dos veces la misma relación si ya se hizo por filtros
            # NOTA: join() en SQLAlchemy es idempotente si es la misma entidad/atributo
            queryset = queryset.join(relation_attr)

        # 6. Combina todos los filtros
        if combined_filters is not None and search_condition is not None:
            queryset = queryset.where(
                and_(combined_filters, search_condition)
            )
        elif combined_filters is not None:
            queryset = queryset.where(combined_filters)
        elif search_condition is not None:
            queryset = queryset.where(search_condition)

        # 6. Resolver y aplicar orden
        order_expression, order_joins = self._resolve_order_by(
            order_by=order_by,
        )

        for relation_name, relation_attr in order_joins.items():
            queryset = queryset.join(relation_attr)

        if order_expression is not None:
            queryset = queryset.order_by(order_expression)

        queryset = self._apply_joins(queryset, joins)

        return queryset

    def build_list_queryset(
        self,
        **kwargs: Any,
    ) -> Select[Tuple[ModelT]]:
        """
        Construye el query inicial para listado.
        Debe retornar un objeto Select.

        Hook de scoping row-level (multi-tenant): sobrescríbelo para restringir
        QUÉ filas ve cada usuario ANTES de la paginación::

            def build_list_queryset(self, **kwargs):
                q = select(self.model)
                user = getattr(self.service.request.state, "user", None)
                if user:
                    q = q.where(self.model.company_id == user.company_id)
                return q
        """
        return select(self.model)

    async def list_paginated(
        self,
        page: int = 1,
        count: int = 25,
        filters: Optional[Dict[str, Any]] = None,
        use_or: bool = False,
        joins: Optional[List[str]] = None,
        order_by: Optional[Any] = None,
        search: Optional[str] = None,
        search_fields: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Tuple[List[ModelT], int]:
        """MOTOR DE PAGINACIÓN — NO LO REIMPLEMENTES.

        Este es el único loop de paginación (count + offset/limit + hidratación
        de columnas). Copiarlo/reescribirlo en un service o controller es EL
        anti-patrón #1 de esta lib. Para personalizar un listado, override el
        HOOK correcto y deja que este método pagine:

        - query base / joins / scoping / columnas / rangos → `build_list_queryset`
        - filtros que dependen del usuario/acción            → `Service.get_filters`
        - orden por defecto                                  → `Service.order_by` / `get_order`
        - joins por acción                                   → `Service.get_kwargs_query` + `self.action`
        - enriquecer los items de la página                 → `Service.post_process_list`

        Si ves `func.count(` o `.offset(` dentro de un método `list_*` propio,
        estás reescribiendo esto — casi siempre es el hook equivocado.
        """
        # 1. Construir query base
        query_kwargs = {
            "filters": filters,
            "use_or": use_or,
            "joins": joins,
            "order_by": order_by,
            "search": search,
            "search_fields": search_fields,
        }
        try:
            # Intentar con argumentos (subclases modernas)
            queryset = self.build_list_queryset(**query_kwargs)
        except TypeError:
            # Fallback sin argumentos (subclases legacy)
            queryset = self.build_list_queryset()

        # 2. Aplicar filtros estándar
        queryset = self.apply_list_filters(
            queryset=queryset,
            filters=filters,
            use_or=use_or,
            joins=joins,
            order_by=order_by,
            search=search,
            search_fields=search_fields,
        ) 
        # Count total
        db = self.session

        # Para contar, hacemos un subquery para asegurar que contamos filas únicas
        count_query = select(func.count()).select_from(queryset.subquery())
        total = (await db.execute(count_query)).scalar_one()

        # Page items
        offset = count * (page - 1)
        page_query = queryset.offset(offset).limit(count)
        result = await db.execute(page_query)
        rows = result.unique().all()

        # Procesar filas para soportar "Result Hydration"
        items = []
        for row in rows:
            # Si la fila tiene un solo elemento, tratarlo como escalar normal
            if len(row) == 1:
                items.append(row[0])
            else:
                # Fila compleja: tomar el primer elemento como entidad
                entity = row[0]

                column_keys = list(row._mapping.keys())

                for i in range(1, len(row)):
                    if i < len(column_keys):
                        key = column_keys[i]
                    else:
                        key = f"_extra_{i}"

                    value = row[i]
                    setattr(entity, key, value)

                items.append(entity)

        return items, int(total)

    async def update(
        self,
        record_id: Union[str, UUID],
        update_data: Dict[str, Any],
    ) -> ModelT:
        """Actualiza un registro por ID con los campos provistos.

        NOTA de divergencia entre ORMs: la firma SQL es ``update(id, dict)`` y
        OMITE valores ``None`` (no puede setear una columna a NULL por acá). El
        repo Beanie usa ``update(obj, dict)`` (recibe el Document) y sí setea
        ``None``. Ver la tabla en el CLAUDE.md de la lib.
        """
        if not self.model:
            raise ValueError("El modelo no está definido en el repositorio")
        db = self.session
        record = await db.get(self.model, record_id)
        if not record:
            raise NotFoundException(
                message=f"{self.model.__name__} no encontrado"
            )
        for key, value in update_data.items():
            if value is not None and hasattr(record, key):
                setattr(record, key, value)
        await db.flush()
        await db.refresh(record)
        return record

    async def delete(
        self,
        record_id: Union[str, UUID],
        model: Optional[Type[Any]] = None,
    ) -> bool:
        """Elimina un registro por ID."""
        model = model or self.model
        if not model:
            raise ValueError("El modelo no está definido en el repositorio")
        db = self.session
        record = await db.get(model, record_id)
        if not record:
            raise NotFoundException(message=f"{model.__name__} no encontrado")
        await db.delete(record)
        await db.flush()
        return True

    async def save(self, obj: ModelT) -> ModelT:
        """Persiste una entidad nueva o mutada (add + flush)."""
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def hard_delete(self, obj: ModelT) -> None:
        """Elimina físicamente la fila (sin soft delete)."""
        await self.session.delete(obj)
        await self.session.flush()