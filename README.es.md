# sql-table-swiss-knife

**Edita tablas de catálogo sin escribir SQL — y siempre ves el SQL que ejecuta.**

Una aplicación de terminal (TUI) en Python 3.14 para **ver y editar filas de tablas de base
de datos que no tienen interfaz CRUD**: códigos de estado, listas de tipos, tablas de
parámetros, *feature flags*. Prepara las ediciones en memoria, muestra el SQL exacto que
se ejecutará (parametrizado y también como literal listo para copiar) y las aplica en una
única transacción. Primer DBMS objetivo: Microsoft SQL Server.

[English](README.md) · **Español**

[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/downloads/)
[![TUI](https://img.shields.io/badge/TUI-Textual-2b6cb0.svg)](https://textual.textualize.io/)
[![DBMS: SQL Server](https://img.shields.io/badge/DBMS-SQL%20Server-CC2929.svg)](https://learn.microsoft.com/sql/sql-server/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![status: beta](https://img.shields.io/badge/status-beta-orange.svg)](PROGRESS.md)

### Para quién es

Si hoy mantienes una de esas tablas, probablemente abres `psql` o `sqlcmd` y escribes un
`UPDATE` a mano — o no lo haces, y la tabla se desincroniza en silencio. Esta es la
herramienta para mantenerla sin el SQL.

**No** es un IDE de base de datos. No hay consola SQL libre, ni DDL, ni diseñador de
esquemas, ni ORM. Si quieres eso, usa las herramientas de tu propia base de datos — esto
es deliberadamente la cosa pequeña.

### Qué hace

- **Las ediciones se preparan, nunca son inmediatas.** Nada llega a la base de datos hasta
  que pulsas Apply, y Apply lo ejecuta todo en una transacción: todo, o nada.
- **El SQL está siempre en pantalla** (`F3`), tanto como se envía como como un script
  literal listo para copiar. Este es el objetivo de la herramienta: no tienes que *escribir*
  SQL, pero siempre puedes *leerlo* — y copiar el script para que un DBA lo ejecute por
  fuera.
- **Seguro por defecto.** Solo lectura está activo por defecto en perfiles de producción;
  una insignia roja `PROD` en la cabecera; el diálogo de Apply indica las cantidades y las
  tablas afectadas y exige escribir una palabra contra producción; cada Apply —confirmado,
  revertido o rechazado— queda registrado en un registro de auditoría local.
- **Rechaza en lugar de adivinar.** Una tabla sin clave primaria es de solo lectura,
  porque un `UPDATE` sin clave es `WHERE 1=1` por accidente.
- **Los datos incómodos están previstos.** Las tablas anchas se desplazan con la columna
  clave congelada; el texto largo se abre a pantalla completa; el binario muestra un volcado
  hexadecimal; una conexión caída te dice que tu trabajo preparado está a salvo en lugar de
  perderlo.

> Este proyecto se gestiona con [**uv**](https://docs.astral.sh/uv/) — `uv sync`,
> `uv add`, `uv run`. No hagas `pip install` en el venv; no hay `requirements.txt`.

Estado: **Hito 8 — robustez y publicación.** Ediciones preparadas con vista previa del SQL en
vivo, importación/exportación desde el portapapeles, capa de seguridad, manejo de celdas
anchas/largas/binarias, un aviso de reconexión que conserva tu trabajo preparado, una
pantalla de ayuda `F1` y el empaquetado. Consulta [PROGRESS.md](PROGRESS.md) para el estado
completo, [SPEC.md](SPEC.md) / [DESIGN.md](DESIGN.md) para requisitos y diseño,
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) para las capas y
[docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md) para soportar otro DBMS.

## Capturas

Todas las imágenes son **instantáneas SVG de Textual** de pantallas reales, exportadas
directamente de `tests/tui/__snapshots__/`. Las genera la suite de pruebas, así que no pueden
desviarse del código sin que una prueba falle.

**Perfiles de conexión** — el entorno de cada perfil es visible antes de conectar.

![Perfiles de conexión](docs/screenshots/connections_screen_with_profiles.svg)

**Explorador de tablas** — árbol de esquemas con las insignias: `🔑` clave primaria,
`🔗` claves foráneas, `⚡` triggers, `⚠` **sin** clave primaria (solo lectura), `👁` vista.

![Explorador de tablas](docs/screenshots/table_browser.svg)

**El espacio de trabajo** — rejilla de datos, inspector y (con `F3`) el panel SQL. Fíjate en
la columna clave congelada y en las insignias de columna: tipo, `🔒` solo lectura, `✱`
actualizada, `∅` admite nulos.

![Espacio de trabajo de tabla](docs/screenshots/table_editor_split_view.svg)

**Tablas anchas** se desplazan horizontalmente con la columna de identidad congelada.

![Tabla ancha con clave congelada](docs/screenshots/wide_table_frozen_key.svg)

**Apply contra producción** — borde rojo, las cantidades, las tablas afectadas, la garantía
de transacción y una palabra que hay que escribir.

![Confirmación de Apply en producción](docs/screenshots/apply_confirmation_production.svg)

**Vista expandida de celda** — una celda de texto largo completa, con ajuste duro y solo
lectura.

![Vista expandida de celda](docs/screenshots/cell_expand_view.svg)

**Conexión perdida** — el aviso que dice que tus cambios preparados están a salvo.

![Aviso de reconexión](docs/screenshots/connection_lost_reconnect_prompt.svg)

**Sesión de solo lectura** — la insignia lleva `RO` y desaparecen los avisos de escritura.

![Sesión de solo lectura](docs/screenshots/read_only_session.svg)

**Ayuda `F1`** — todos los atajos de teclado, agrupados, con el resumen de seguridad.

![Pantalla de ayuda](docs/screenshots/help_screen.svg)
## Instalación

```bash
uv tool install sql-table-swiss-knife     # desde PyPI
# o, desde una copia del repositorio
git clone https://github.com/Chienwei82/sql-table-helper.git
cd sql-table-helper
uv sync
uv run sql-table-swiss-knife
```

Requiere Python 3.14. Para conectarte también necesitas un **gestor de drivers ODBC** y el
**Microsoft ODBC Driver 18** (17 también funciona) — en Linux, `unixODBC` más el driver.

También se admite una compilación de **un solo archivo** para máquinas restringidas y para
dejar la aplicación en un servidor sin tocar su Python:

```bash
uv run scripts/build_standalone.py        # escribe dist/sql-table-swiss-knife (un archivo)
```

Usa [PyInstaller](https://pyinstaller.org) (declarado como extra opcional `standalone`) y
**no** se compila ni se prueba en CI — el binario congelado es específico de cada plataforma
y la suite de pruebas no lo cubre. Consulta
[Limitaciones conocidas](#limitaciones-conocidas).

## Desarrollo

```bash
uv sync                     # instala dependencias de ejecución y desarrollo (Python 3.14)
uv run pytest               # pruebas unitarias + de proveedor + de TUI (las live se omiten solas)
uv run pytest -m live       # pruebas de integración (necesitan el servidor docker, ver abajo)
uv run ruff check .         # lint
uv run ruff format --check .  # comprobación de formato
uv run mypy                 # comprobación de tipos estricta
uv run sql-table-swiss-knife --version
uv run sql-table-swiss-knife   # iniciar la TUI (salir: ctrl+q)
```

## Uso de la TUI

| Tecla | Dónde | Acción |
|---|---|---|
| `n` / `e` / `d` / `x` | conexiones | crear / editar / duplicar / eliminar un perfil |
| `t` | conexiones | probar el perfil (no deja ninguna sesión abierta) |
| `enter` | conexiones | conectar — aparece el selector de base de datos si el perfil no tiene una por defecto |
| `b` | conexiones | cambiar de base de datos en la sesión activa |
| `/` | tablas | búsqueda mientras escribes sobre `schema.table` |
| `f6` | tablas | plegar/desplegar los grupos de esquemas |
| `enter` | tablas | abrir la tabla seleccionada (rejilla + inspector) |
| `f2` | tabla | mostrar/ocultar el panel del inspector |
| `f3` | tabla | mostrar/ocultar el **panel SQL** (F3) |
| `v` | tabla | alternar el renderizado del SQL: parametrizado → literal → script |
| `y` | tabla | copiar el SQL — el script entero, o la sentencia seleccionada |
| `ctrl+c` | tabla | copiar la celda / fila / columna / selección en el formato actual |
| `b` | tabla | alternar el alcance de copia: celda → fila → columna → selección |
| `p` | tabla | alternar el formato de copia TSV → CSV → JSON (recordado en settings.toml) |
| `ctrl+v` | tabla | pegar (el pegado entre corchetes del terminal es la vía principal) |
| `i` / `o` | tabla | importar un archivo CSV/JSON / exportar las filas en pantalla |
| `g` | tabla | "generar SQL para…" esta fila o el filtro actual |
| `m` | tabla | traer la siguiente página de filas (página de 1000 filas por defecto) |
| `r` | tabla | recargar metadatos y filas |
| flechas / `enter` | tabla | mover el cursor de celda (el inspector sigue) / explicar la celda |
| `ctrl+p` | en cualquier sitio | paleta de comandos (búsqueda difusa sobre las acciones de la pantalla actual) |
| `f1` (o `?`) | en cualquier sitio | la pantalla de ayuda: todos los atajos, agrupados |
| `f5` | tabla | alternar el modo de solo lectura para esta sesión |
| `ctrl+s` | tabla | aplicar todos los cambios preparados (tras la confirmación) |
| `w` | tabla | **expandir** la celda enfocada — texto completo, o volcado hexadecimal si es binario |
| `ctrl+t` | en cualquier sitio | cambiar de tema (persistido en `settings.toml`) |
| `esc` | en cualquier sitio | retroceder un nivel |
| `ctrl+q` | en cualquier sitio | salir (primero se cierra la conexión) |

La lista completa y autorizada está dentro de la propia aplicación: pulsa `F1`. La tabla
anterior es una comodidad, y `tests/unit/tui/test_keybindings.py` falla si las dos divergen.

### Reasignar las teclas

En la primera ejecución se escribe una plantilla `keybindings.toml` vacía en el directorio
de configuración. Muestra la ruta con `--print-config-dir`, edita el archivo, reinicia:

```bash
uv run sql-table-swiss-knife --print-config-dir
# → /home/you/.config/sql-table-swiss-knife
```

```toml
[keys]
apply = "ctrl+g"          # en lugar de ctrl+s
help  = "f2"              # en lugar de f1
```

Los nombres de acción desconocidos, los valores que no son cadenas y el TOML mal formado se
**informan como avisos en la línea de estado** y se conservan los valores por defecto — un
archivo de preferencias opcional nunca debe impedir que la aplicación arranque.

Las filas llevan insignias que sobreviven a cualquier tema y a cualquier diferencia de
visión de color: `🔑` tiene clave primaria, `🔗` tiene claves foráneas, `⚡` tiene triggers,
`⚠` **no** tiene clave primaria (las filas son de solo lectura, S-4) y `👁` es una vista.

La misma convención recorre el espacio de trabajo de la tabla:

- **Las cabeceras de la rejilla** muestran el nombre de la columna, sus insignias y el tipo
  exacto — p. ej. `Code 🔑✱ char(2)`. `🔒` marca una columna de solo lectura (identity,
  calculada, rowversion).
- **El panel del inspector** (`F2`) tiene cuatro secciones: TABLE SUMMARY (schema.name,
  filas, PK, "referenciada por N tablas"), un banner WARNINGS coloreado por gravedad
  (`⛔` / `⚠` / `•`, triggers deshabilitados atenuados), el COLUMN LIST con insignias por
  columna (`🔑` PK con su ordinal en una clave compuesta, `🔗` FK → destino, `#` identity,
  `ƒ` calculada, `⏱` rowversion, `∅` admite nulos / `✱` obligatoria, `D` default, `U`
  única, `✓` check) y el COLUMN DETAIL de la columna enfocada (tipo exacto, nulabilidad,
  expresión por defecto, seed/increment de identity, definición calculada, texto del
  check, nombres de índices únicos, acciones referenciales de la FK, collation).
- **El riesgo primero.** Un trigger `INSTEAD OF` indica que tu INSERT/UPDATE/DELETE puede
  no hacer lo que esperas; una tabla sin clave indica que la identidad de fila es ambigua;
  las claves foráneas entrantes indican que borrar filas puede fallar o propagarse, con las
  acciones referenciales explícitas. Los riesgos de CHECK y UNIQUE se marcan como "los
  verificará la base de datos" — la aplicación nunca los evalúa por su cuenta.
## Copiar y pegar

**Copiar** pone en el portapapeles la celda, la fila entera, la columna entera o el
rectángulo seleccionado como TSV (por defecto — se pega directamente en Excel y Sheets),
CSV (citado según RFC 4180) o JSON, con `NULL` escrito como texto vacío o como la palabra
literal (`copy_null_repr`). El alcance es explícito (`b`) porque "copiar esta fila" y
"copiar el INSERT de esta fila" son peticiones distintas y ninguna debería adivinarse.

El mecanismo es una cadena: **pyperclip** si está instalado el extra opcional, si no el
comando propio de la plataforma (`pbcopy`, `wl-copy`/`xclip`/`xsel`, `clip.exe`) y
finalmente la secuencia de escape **OSC 52** — que es lo que mantiene el copiado
funcionando por SSH y dentro de tmux, donde no existe portapapeles del sistema. La línea de
estado nombra el mecanismo que tomó el texto, y una copia fallida se informa en lugar de
darse por supuesta.

**Pegar** se lee del *bracketed paste* del terminal, así que un bloque copiado de Excel
llega intacto como un único evento. Se analiza (BOM/CRLF normalizados, citado RFC 4180 para
que una celda pueda contener tabuladores y saltos de línea, se aceptan arrays JSON de
objetos) y luego se **planifica**:

- **un valor** → la celda enfocada, con sus saltos de línea;
- **un bloque rectangular sobre una selección** → esa selección, un valor rellenándola
  entera;
- **cualquier cosa con varias filas** → filas: `UPDATE` donde la clave primaria coincide con
  una fila cargada, `INSERT` donde no.

Un diálogo de **Vista previa del pegado** muestra entonces el mapeo de columnas (por nombre
de cabecera cuando el bloque la trae, posicional si no), el valor convertido junto al texto
pegado (`31.01.2026 → 2026-01-31`, `1.234,56 → 1234.56`), el reparto UPDATE/INSERT y cada
error de validación por celda. **No se prepara nada hasta que se confirma ese diálogo**, y un
plan con una sola celda mala se rechaza de plano — no existe un "pegar solo las filas
válidas".

La importación desde y la exportación a **archivos CSV/JSON** pasan por exactamente el
mismo analizador, convertidor y vista previa, así que un archivo nunca puede validarse de
otra manera que un pegado — y una importación nunca escribe en la base de datos.

Los ajustes viven en `settings.toml`:

```toml
copy_format = "tsv"                # tsv | csv | json
copy_null_repr = ""                # cómo se copia NULL
paste_null_token = "NULL"          # qué significa NULL de SQL al pegar ("" lo desactiva)
paste_null_as_literal = false      # true: la palabra "NULL" se pega como texto
paste_number_locale = "en"         # en (1,234.56) | de (1.234,56)
paste_date_format = "iso"          # iso | dmy | mdy
paste_max_rows = 5000              # rechaza un bloque mayor en lugar de prepararlo
clipboard_read_fallback = false    # permite que ctrl+v lea el portapapeles del sistema
```


## Seguridad

Una herramienta que escribe en una base de datos tiene que ser *aburrida* al respecto. Cada
regla de aquí existe porque la alternativa es un error plausible contra datos reales.

### Solo lectura por perfil, activo por defecto en producción

Cada perfil declara un **entorno** y una bandera de solo lectura:

```toml
[[profile]]
name = "prod-erp"
environment = "production"   # development | test | staging | production
read_only = true             # opcional; si se omite, decide el entorno
```

Si omites `read_only` decide el entorno: **producción se abre en solo lectura**, todo lo demás
se abre con escritura. Un perfil de producción que pueda escribir por accidente es el fallo
que esto evita, así que la respuesta segura es el valor por defecto y activar la escritura
es siempre un acto explícito y visible. `f5` lo alterna para la sesión; la insignia cambia
de inmediato. `sql-table-swiss-knife --read-only` fuerza solo lectura para todo el proceso y
**bloquea el alternador** — una sesión de solo lectura de la que no puedes salir por
casualidad.

El modo de solo lectura se aplica en el límite de la *preparación*, no en Apply: las
ediciones se rechazan donde se habrían hecho, así que una sesión de solo lectura nunca
acumula trabajo que no se le permitirá confirmar.

### La insignia de entorno

La cabecera lleva una píldora grande de color — `PROD` rojo, `STAGING` ámbar, `TEST` azul,
`DEV` verde — más `RO` cuando la sesión es de solo lectura. Es una palabra y no solo un
color, porque "¿en qué base de datos estoy?" y "¿puedo escribir?" son dos preguntas
distintas y el color por sí solo no responde a ninguna de forma fiable.

La insignia la gobierna el *mismo* objeto `SafetyPolicy` que controla la escritura. Una
insignia que dijera `DEV` mientras el diálogo de Apply exige una palabra de producción sería
peor que no tener insignia, así que ambas son estructuralmente incapaces de discrepar — y
hay una prueba que lo fija.

### La confirmación de Apply

`ctrl+s` nunca escribe directamente. El diálogo declara lo que va a pasar:

- las **cantidades** por tipo — `About to apply: 1 insert, 2 updates, 0 deletes`;
- las **tablas afectadas** — una cantidad sola no te dice *qué* tabla estás a punto de
  cambiar;
- la **garantía de transacción** — todas las sentencias se ejecutan en una transacción, y
  cualquier fallo revierte todo;
- para **producción** (y para lotes grandes de borrado), una **confirmación escrita**: el
  botón permanece deshabilitado hasta que escribes la palabra exacta. Tanto `y` como
  `enter` vuelven a comprobarla, así que el aviso no se puede esquivar.

### Rechazos

| Rechaza | Por qué | Cómo anularlo |
|---|---|---|
| editar una tabla **sin clave primaria** | un `UPDATE`/`DELETE` sin clave es `WHERE 1=1` por accidente | `allow_keyless_writes` en la configuración |
| `UPDATE`/`DELETE` sin columnas clave preparadas | el guardián de concurrencia optimista no tiene con qué comparar | — |
| escribir a través de una **sesión de solo lectura** | lo dice el perfil/entorno | `f5`, o `--read-only` es definitivo |
| escribir en una **vista** | no tiene columnas actualizables | — |
| una **columna gestionada por el servidor** (identity, calculada, rowversion) | el servidor es su dueño | INSERT con valores de identity, tras un aviso |
| un **trigger deshabilitado** en la tabla | no se disparará, así que el cambio no es lo que el esquema implica | se muestra como aviso en el inspector |

### El registro de auditoría

Cada Apply —confirmado, **revertido** o **rechazado**— añade una línea JSON a
`audit.log.jsonl` en el directorio de configuración:

```json
{"timestamp": "2026-03-04T09:12:33Z", "profile": "prod-erp", "environment": "production",
 "server": "sql01.corp", "database": "ERP", "table": "dbo.Country",
 "counts": {"insert": 0, "update": 1, "delete": 0},
 "statements": ["UPDATE dbo.Country SET [Name] = N'Germany (edited)' WHERE [Code] = N'DE';"],
 "outcome": "committed", "duration_ms": 42}
```

**Ninguna contraseña, nunca** — ni la contraseña de conexión, ni una cadena de conexión. Las
sentencias son el renderizado *literal*, porque el sentido del registro es que alguien que
audite el cambio seis meses después pueda leerlo sin los valores de los parámetros. Los
Apply rechazados también se registran: "alguien intentó cambiar producción a las 09:12" es
justo el evento que un registro de auditoría existe para sacar a la luz.

## Datos grandes, anchos e incómodos

La herramienta se usa contra las tablas a las que nadie dio una interfaz — que son, por
definición, las incómodas.

- **Tablas enormes** se paginan (`m` trae las siguientes 1000 filas; `fetch_limit` en
  `settings.toml`), y ninguna operación lanza jamás `SELECT *` sobre toda la tabla.
- **Tablas anchas** se desplazan horizontalmente, y la columna de identidad queda
  **congelada** — con cien columnas sigues sabiendo qué fila estás mirando.
- **Celdas de texto largo** se truncan en la rejilla con un marcador y se abren a **pantalla
  completa** con `w`, con ajuste duro y en solo lectura. La rejilla sigue siendo una rejilla.
- **Celdas binarias** muestran una vista previa hexadecimal en la celda (`0x0102… (8 bytes)`)
  y un volcado hexadecimal completo en la vista expandida. Nunca se muestran como
  caracteres basura.
- **La pérdida de conexión** es un estado de primera clase, no un fallo: un enlace caído lanza
  un **aviso de reconexión** que te dice que tus cambios preparados siguen ahí. Al reconectar
  se descarta la caché de catálogo muerta y se vuelven a leer las filas; el búfer de
  preparación se reengancha, así que una red inestable no puede costarte una tarde de
  ediciones.

La decisión sobre el tipo de celda vive en `services/cellview.py` como funciones puras sobre
valores, así que "¿es esta celda expandible y cómo?" se prueba con pruebas unitarias en vez
de afirmarse a través de un widget.

## El panel SQL

`F3` abre un panel bajo la rejilla que muestra el SQL de cada cambio pendiente, en orden de
aplicación, con resaltado de sintaxis SQL. Es el sentido de la herramienta: la aplicación
existe para que no tengas que *escribir* SQL, y aquí es donde aún puedes *verlo*.

**Tres renderizados, una tecla (`v`).** Son tres vistas de los mismos objetos de sentencia,
así que nunca pueden discrepar sobre lo que haría la aplicación:

| Modo | Muestra | Úsalo para |
|---|---|---|
| **Parametrizado** | `UPDATE … SET [Name] = @p0 …` más una leyenda (`@p0 = N'Alemania'`) | ver exactamente lo que se envía al servidor |
| **Literal** | la misma sentencia con los valores en línea y escapados | pegar en SSMS, en otra herramienta, en un ticket |
| **Script** (por defecto) | todas las sentencias dentro de `BEGIN TRANSACTION` / `BEGIN TRY … END TRY BEGIN CATCH … END CATCH` / `COMMIT`, con `SET IDENTITY_INSERT` cuando el script lo necesita | ejecutar el conjunto de cambios a mano, todo o nada |

El renderizado de script es el que abre el panel, porque es el que es seguro de ejecutar sin
pensar. Hace `SET XACT_ABORT ON`, revierte y vuelve a lanzar con `THROW` en el `CATCH`, y
protege la reversión con `IF @@TRANCOUNT > 0` para que un error *anterior* a la apertura de
la transacción no quede enmascarado por un segundo.

**Copiar (`y`).** Sin ninguna sentencia seleccionada obtienes el script entero; con una
seleccionada obtienes esa sentencia (en modo parametrizado, junto con sus valores de
parámetro — un `@p0` suelto no es algo que puedas pegar).

**Nada se ejecuta desde aquí.** El panel es solo de vista previa y lo dice en su cabecera. El
manejo de `SET`/`IDENTITY_INSERT` y todas las acciones de "generar" producen *texto*; lo
único en la aplicación que escribe es Apply (`ctrl+s`), tras una confirmación.

**"Generar SQL para…" (`g`)** produce una sentencia para la fila enfocada o para el filtro
actual, para los casos en que necesitas SQL que no es un cambio pendiente:

- `SELECT` / `INSERT` / `UPDATE` / `DELETE` para la fila enfocada;
- `MERGE` — un *upsert* emparejado por clave primaria, para que mover una fila entre entornos
  actualice la fila que ya está ahí en lugar de chocar con la clave;
- `INSERT script for all rows in this table/filter` — un único `INSERT` por lotes de
  múltiples filas para todas las filas cargadas, que es la forma de mover una tabla de
  catálogo a otro entorno. Las columnas calculadas, rowversion e identity se omiten: el
  entorno de destino conserva las suyas.

Solo se ofrecen las sentencias que pueden ser *correctas* para la fila actual, y se muestra
el motivo de cada omisión (sin clave → no hay `UPDATE`/`DELETE`; sin clave primaria → no hay
`MERGE`).

### Escapado

Todo vive en un solo sitio — `SqlDialect.literal()` — y está cubierto por
`tests/unit/test_dialect_escaping.py`:

- cadenas → `N'…'` con `'` duplicado (el prefijo `N` es lo que sobrevive a una colación no
  Unicode), los saltos de línea y caracteres de control se mantienen literales;
- `None` → `NULL`, `bool` → `1`/`0`, `bytes` → `0x…`, `Decimal` → dígitos pelados (nunca
  `1E+3`), fechas/horas → ISO-8601, `UUID` → su forma textual;
- un entero fuera del rango de `bigint` y una cadena con un NUL se **rechazan** en vez de
  renderizarse: un literal que SQL Server interpretaría mal es peor que un error claro.

## Temas

Tres integrados — `default-dark`, `light` y `high-contrast` — conmutables en ejecución con
`ctrl+t` o desde la paleta de comandos, y la elección se persiste en `settings.toml`:

```toml
theme = "high-contrast"
fetch_limit = 1000          # otros ajustes de DESIGN §13.2 también se leen aquí
```

Todos los temas declaran la misma paleta *semántica* (`$pk`, `$fk`, `$identity`, `$computed`,
`$nullable`, `$error`, `$warning`, `$pending`), así que la interfaz nunca fija un color en
código.

## Perfiles de conexión

Los perfiles viven en el directorio de configuración de platformdirs (con
`SWISSKNIFE_CONFIG_DIR` se sobrescribe) como `profiles.toml`. **Las contraseñas nunca se
escriben en ese archivo** — viven en el almacén de claves del sistema operativo (servicio
`sql-table-swiss-knife`, cuenta = `secret_ref`) o se piden en cada sesión cuando no hay
ningún backend de keyring disponible:

```toml
[[profile]]
name = "local"
provider = "mssql"
host = "localhost"
port = 1433
database = "SwissKnifeSample"
auth = "sql"                  # o "integrated" para autenticación integrada de Windows
username = "sa"
secret_ref = "local@localhost"   # cuenta del keyring, NO la contraseña
[profile.options]
encrypt = true
trust_server_certificate = true
driver = "ODBC Driver 18 for SQL Server"
```

Una clave `password`/`pwd` en ese archivo se rechaza al cargar.

## SQL Server de ejemplo

En `tests/live` hay un SQL Server 2022 (Developer) en docker con una base de datos de
muestra tipo catálogo:

```bash
cd tests/live
docker compose up -d          # arranca SQL Server y luego carga init/01_sample_catalog.sql
docker compose down -v        # borrar
```

Conexión: `localhost,1433`, usuario `sa`, contraseña `SwissKnife!2022_Test` (se sobrescribe
con `MSSQL_SA_PASSWORD`), base de datos `SwissKnifeSample`.

Ejecutar pyodbc en Linux necesita además `unixODBC` y el Microsoft ODBC Driver 18.

## `inspect` — verificar la introspección desde la terminal

Un comando temporal del Hito 2 que imprime los metadatos introspeccionados como tablas
Rich, para que puedas comprobar la capa `sys.*` sin lanzar la TUI:

```bash
uv run sql-table-swiss-knife inspect <profile> dbo.Region     # metadatos completos
uv run sql-table-swiss-knife inspect <profile> x --list       # tablas + recuentos de filas
uv run sql-table-swiss-knife inspect <profile> x --databases  # bases de datos
```

Resuelve la contraseña desde el keyring, preguntando si hace falta. Se espera que este
comando se reescriba o se elimine: el inspector de la TUI muestra los mismos metadatos de
forma interactiva.

## Estructura

```
src/sql_table_swiss_knife/
  domain/      modelos puros — ConnectionProfile, Table, PendingChange, RowKey (sin E/S)
  providers/   protocolos DatabaseProvider + SqlDialect, generación de SQL compartida,
               registro de plugins y mssql/ (cadena de conexión, mapeo de errores,
               metadatos sys.*)
  storage/     profiles.toml (sin secretos), settings.toml, keyring + almacenes de secretos
               efímeros, audit.log.jsonl
  services/    la capa con la que habla la TUI: ciclo de vida de la conexión, lecturas de
               catálogo, paginación, preparación, validación, política de seguridad, vista
               previa del SQL, vistas de celda, portapapeles
  tui/         aplicación Textual: screens/, widgets/, theme.py, keybindings.py, keymap.py
  infra/       backends de portapapeles y traducción de errores
docs/          ARCHITECTURE.md, ADDING_A_PROVIDER.md, screenshots/ (SVG generados)
scripts/       build_standalone.py (compilación opcional de un solo archivo)
tests/
  unit/        domain, services, providers, storage — sin terminal, sin base de datos
  tui/         pruebas con Textual Pilot + __snapshots__/ instantáneas SVG
  live/        integración contra el SQL Server en docker
```

Las capas las hace cumplir `import-linter` en tiempo de CI; un módulo que salta de capa
hace fallar la compilación en lugar de una revisión de código.

## Pruebas y puertas de calidad

```bash
uv run pytest                    # todo; las pruebas live se omiten solas
uv run pytest tests/unit -q      # rápido: sin terminal, sin base de datos
uv run pytest --cov=sql_table_swiss_knife --cov-report=term-missing
uv run ruff check . && uv run ruff format --check .
uv run mypy                      # estricto, src + tests
```

Las capas se prueban deliberadamente de forma desigual: las decisiones (seguridad,
preparación, generación de SQL, tipado de celdas, análisis del keymap) están cubiertas
densamente porque ahí es donde un bug es caro; la fontanería de los widgets se cubre con
pruebas de Pilot e instantáneas, que detectan los fallos que realmente ocurren ahí. Las
cifras exactas y la cobertura están en [PROGRESS.md](PROGRESS.md).

## Limitaciones conocidas

 Dichas con franqueza, porque una lista de ellas es más útil que una afirmación de
completitud:

- **La selección es solo con teclado.** `shift`+flechas funciona; la selección arrastrando
  con el ratón no está conectada a la rejilla, así que `ctrl+c` con el alcance *selección*
  necesita el teclado.
- **No hay una pasada en vivo contra una base de datos real.** Todo está cubierto por
  pruebas unitarias y de Pilot contra `FakeProvider`. El proveedor mssql tiene pruebas
  unitarias de su SQL y del mapeo de metadatos, y `tests/live/` existe para un servidor
  real, pero la ruta completa de Apply nunca se ha ejecutado de extremo a extremo contra
  SQL Server en este entorno. **Hazlo antes de confiarle datos de producción.**
- **La compilación de un solo archivo no está verificada en CI.** La salida de PyInstaller
  es específica de cada plataforma y la suite de pruebas no la cubre. Tampoco incluye
  unixODBC ni el driver ODBC de Microsoft — hay que instalarlos en la máquina de destino.
- **Un solo DBMS.** SQL Server. Las costuras para otros están reales y documentadas
  ([docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md)), pero no se ha escrito ningún
  segundo proveedor contra ellas, así que trata esa guía como un diseño, no como una receta
  probada.
- **El registro de auditoría es un archivo local.** Es de solo anexado por convención
  únicamente — cualquiera con acceso de escritura al directorio de configuración puede
  editarlo. Es un registro de lo que hizo la herramienta, no un sistema de cumplimiento.
- **La comprobación de deriva de atajos cubre las pantallas integradas.** Un atajo
  reasignado por el usuario se valida, pero no se contrasta con los widgets, así que un
  atajo personalizado todavía puede ocultar uno integrado.
- **No se admite la importación de `.xlsx` de Excel.** Sí la importación de archivos CSV y
  JSON, a través del mismo analizador y vista previa que un pegado desde el portapapeles.
  El `.xlsx` en sí no se lee — el CSV exportado desde Excel funciona.
- **La importación/exportación basada en archivos está construida pero SPEC §2.2 sigue
  listándola como fuera de alcance.** El código y la especificación discrepan; la
  especificación aún no se ha corregido. Trata el README como lo vigente.
- **`inspect` es un comando temporal de desarrollo** y se reescribirá o se eliminará.

## Documentación

| Documento | Qué contiene |
|---|---|
| [README.es.md](README.es.md) | esta traducción al español |
| [SPEC.md](SPEC.md) | requisitos (identificadores FR/S/NFR) y las reglas de seguridad (S-*) |
| [DESIGN.md](DESIGN.md) | las decisiones de diseño detrás de esos requisitos |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | las capas, el modelo de seguridad, la concurrencia, la estrategia de pruebas |
| [docs/ADDING_A_PROVIDER.md](docs/ADDING_A_PROVIDER.md) | guía paso a paso para soportar otro DBMS |
| [PROGRESS.md](PROGRESS.md) | estado por hitos, cobertura, huecos conocidos |
| [CONTRIBUTING.md](CONTRIBUTING.md) | cómo contribuir: instalación, las cinco puertas, las reglas que importan |
| [SECURITY.md](SECURITY.md) | cómo informar de una vulnerabilidad, y qué protege y qué no la aplicación |
| [AGENTS.md](AGENTS.md) | **instrucciones para agentes de código con IA** que trabajan en este repositorio |

## Usar agentes de código con IA

[AGENTS.md](AGENTS.md) es el informe autoritativo para un agente que trabaja en este
repositorio: la arquitectura, las cinco puertas de calidad, las reglas que se hacen cumplir
mecánicamente y las convenciones. Es una única fuente de verdad a la que delegan los
archivos por herramienta, así que las reglas no pueden divergir:

| Archivo | Herramienta |
|---|---|
| [AGENTS.md](AGENTS.md) | el propio informe — léelo |
| [CLAUDE.md](CLAUDE.md) | Claude Code (importa `AGENTS.md`) |
| [GEMINI.md](GEMINI.md) | Gemini CLI |
| [.cursorrules](.cursorrules) | Cursor |
| [.github/copilot-instructions.md](.github/copilot-instructions.md) | GitHub Copilot |

La versión corta: solo `uv`; ejecuta `ruff check`, `ruff format --check`, `mypy`,
`lint-imports` y `pytest` antes de decir que has terminado; nunca dejes que `services/`
importe `tui/`; mantén las decisiones en funciones puras; y cada arreglo necesita una
prueba que falle sin él.

## Licencia

MIT — consulta [LICENSE](LICENSE).

Este proyecto se escribió con asistentes de código con IA (Claude Code y similares). La
licencia MIT de arriba es la elección estándar y permisiva y se aplica al código tal como
está; no se usa una licencia separada de "IA", porque no existe ninguna aprobada por la OSI
e inventar una a medida solo haría el código más difícil de reutilizar.

Sobre la autoría: los derechos de autor en el trabajo asistido por IA son un área del
derecho genuinamente sin resolver, y ninguna licencia puede resolverlo. En la práctica, lo
que importa es que la licencia sea estándar para que cualquiera que quiera usarla pueda, y
que la línea de derechos nombre a una persona real que pueda dar permiso.
`Copyright (c) 2026 Chienwei82` cumple ambas cosas. Si representas a una organización en
lugar de a una persona, cambia esa línea por el nombre legal de la organización antes de
publicar.

