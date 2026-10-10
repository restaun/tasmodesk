# TasmoDesk

Panel de escritorio para Windows que descubre, controla, configura y actualiza
dispositivos [Tasmota](https://tasmota.github.io/) de la red local
(`192.168.1.1` a `192.168.1.254`) directamente por HTTP, sin Home Assistant
ni MQTT. Interfaz nativa con PySide6 (Qt Widgets).

## Descargar y ejecutar

Descarga `TasmoDesk.exe` desde la página de
[Releases](https://github.com/restaun/tasmodesk/releases) y ábrelo.
No necesita Python ni instalación. No abre ningún servidor en la red.

Para ver datos de ejemplo sin conectar ni modificar equipos:

```powershell
.\TasmoDesk.exe --demo
```

El modo demo se identifica en pantalla y bloquea las operaciones de red.
El botón **Claro / oscuro** cambia el tema. El tema, la última página y la
preferencia de refresco automático se guardan en
`Documentos\TasmotaManager\preferences.json`; el modo demo no escribe este archivo.
Las credenciales solo permanecen en la memoria del programa.

## Páginas y funciones

- **Dispositivos**: nombres de solo lectura, un botón por salida que muestra
  Encender si está apagada o Apagar si está encendida para los
  relés y deslizador de intensidad de 0–100 % cuando el equipo comunica `Dimmer`.
  La última
  salida corresponde a la luz estándar de estos equipos; no se implementa
  iluminación multicanal independiente.
  La intensidad se envía al soltar el ratón o la tecla; la rueda
  espera 500 ms sin nuevos movimientos antes de enviar un único valor.
- **Configuración**: usuario y contraseña WebUI opcionales, aplicados a todos
  los equipos y mantenidos solo en memoria; información de firmware, hardware,
  actividad y Wi-Fi; edición de DeviceName y FriendlyName; copias manuales.
  Los nombres pendientes no se pierden al refrescar o cambiar de página;
  se indican como **Sin guardar** y pueden descartarse. Son borradores de
  la sesión, no se conservan al cerrar el programa.
- **Actualizaciones**: consulta de la versión estable oficial, selección de
  Español (predeterminado, `tasmota32-ES.bin`) o English (`tasmota32.bin`),
  actualización individual OTA o descarga/subida por archivo, y botón
  **Actualizar todos** mediante archivo, todo en una sola fila.
  El firmware encontrado muestra versión, nombre del archivo, idioma
  seleccionado y dirección OTA oficial.
  **Archivo local…** (para todos) y **Archivo…** (en cada equipo) permiten
  instalar un `.bin` propio; ver «Firmware local».

**Buscar Tasmotas** explora la subred con hasta 16 consultas concurrentes de
solo lectura. Guarda las IP en `Documentos\TasmotaManager\devices.json` y las
carga al arrancar. El primer arranque sin inventario empieza vacío.
Las búsquedas conservan los equipos guardados que no respondan y añaden los
nuevos. Un inventario corrupto impide sobrescribirlo y se comunica el error.
**Refrescar estados** consulta solo los guardados.
El refresco automático consulta cada **60 segundos** y se pausa durante
búsquedas, consultas, cambios y actualizaciones; puede desactivarse en
Configuración. El sondeo de la interfaz no genera consultas continuas a los
dispositivos. Durante el refresco y la consulta de firmware los controles de
cada equipo siguen activos; una lectura que termine después de un cambio no
sobrescribe el estado más reciente. Solo **Buscar Tasmotas** bloquea las tarjetas.

Las tarjetas muestran conexión y fecha de la última lectura correcta,
conservando el nombre conocido si el equipo no responde. El buscador admite
nombre, IP y nombre de salida, con filtros de disponibilidad, salidas apagadas
y firmware pendiente. Cada tarjeta permite **Abrir WebUI** en el navegador.

## Copias y actualización

Antes de cualquier actualización se guarda una copia `.dmp` en
`Documentos\TasmotaBackups`; si falla, ese equipo no se actualiza.
Los scripts Berry y archivos personalizados pueden requerir una copia aparte.
La descarga por archivo valida formato ESP32, tamaño y SHA-256 publicado,
y guarda la imagen en `Documentos\TasmotaFirmware`.
Un lote comparte una sola descarga verificada; cada dispositivo guarda su
copia antes de empezar la preparación y subida. Un fallo de descarga se
comunica a los equipos del lote sin repetirla continuamente.
Configuración permite listar las copias de cada IP y abrir la carpeta de
respaldos, también cuando el equipo esté desconectado. Se reconocen por el
nombre generado con su IP; los archivos renombrados manualmente pueden no
aparecer en el listado.

Las actualizaciones requieren confirmación. El lote incluye los pendientes
compatibles y omite los no disponibles, incompatibles o ya actualizados.
Cada equipo tiene su copia independiente y su registro/progreso en la página
Actualizaciones. Los trabajos se ejecutan en paralelo con hasta 16 trabajadores;
el resumen muestra completados, en curso y errores. El progreso, la ruta de
la copia y el registro completo siguen visibles aunque un equipo no responda
durante su reinicio. Estos registros se mantienen durante la sesión.
El fallo de uno no detiene los demás. Se captura el idioma y las credenciales
al iniciar cada trabajo. No cierres el programa durante una operación.

Solo se ofrece la imagen estándar a equipos ESP32-D0WD-V3 que comuniquen
`release-tasmota32`; la subida por archivo admite también Safeboot.
Si Safeboot impide guardar una copia nueva, se permite seleccionar y autorizar
explícitamente una copia anterior de ese mismo equipo. No se elige automáticamente.
La copia puede no incluir cambios posteriores a su fecha.

OTA usa `OtaUrl` y `Upgrade 1`. Si se detecta Safeboot, se envía una segunda
orden una sola vez. Espera 20 segundos tras la primera orden y 60 tras la
segunda, con sondeos posteriores cada 30 segundos. La subida por archivo
espera 30 segundos y no se repite si se pierde la conexión.
El plazo es de seis minutos por fase.

### Firmware local

El archivo elegido debe ser `.bin` y una imagen OTA válida para ESP32 clásico
(cabecera, chip y descriptor de aplicación; 128 KB–4 MB). La confirmación
muestra tamaño y SHA-256. Al iniciar se comprueba que el archivo no ha cambiado
y se copia a `Documentos\TasmotaFirmware\local-<sha>.bin`; esa copia es la que
se sube. Se aplica a equipos ESP32-D0WD-V3 con Tasmota32 o en Safeboot, con la
misma copia previa y el mismo flujo de Safeboot. Como la imagen no declara la
versión ni el idioma de Tasmota, el éxito se confirma cuando el equipo vuelve a
responder fuera de Safeboot y se registra la versión que comunica.

La barra es una estimación temporal, no el porcentaje real de descarga. Nunca
llega al 100 % sin confirmar la versión objetivo o posterior y el idioma
seleccionado mediante `StatusMEM.Features`. La misma versión puede reinstalarse
para cambiar de idioma si el actual se conoce; no se ofrecen versiones inferiores.

## Seguridad

Usa solo una red local de confianza. La API y la subida al equipo usan HTTP
sin cifrar; no expongas la WebUI a Internet. Las descargas del ordenador usan
HTTPS y SHA-256. Las contraseñas no se guardan ni se registran, pero HTTP no
cifra el tráfico. Los respaldos pueden contener credenciales: protégelos.
Una respuesta de subida exitosa no demuestra la instalación; se comprueba
después del reinicio.

## Fuentes, construcción y pruebas

Requiere Python 3.11:

```powershell
py -3.11 -m pip install -r .\requirements.txt
.\run.bat
.\build.bat
py -3.11 -m unittest -v
```

`run.bat` inicia el programa desde las fuentes y `build.bat` genera
`dist\TasmoDesk.exe` con PyInstaller.
`tasmodesk.py` implementa la interfaz Qt; `tasmota_service.py` contiene las
operaciones y trabajos; `tasmota_manager.py` el cliente HTTP, el inventario y
las validaciones de firmware.

`--smoke` prueba las páginas con datos de demostración y cierra automáticamente.
Las pruebas usan respuestas simuladas y no modifican equipos reales.

## Aviso

Proyecto independiente, no afiliado al proyecto Tasmota. Úsalo bajo tu propia
responsabilidad: una actualización fallida puede requerir recuperar el equipo
por cable serie.

## Apoya el proyecto

Si te resulta útil, puedes invitarme a un café:

[![Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/restaun)

## Licencia

Distribuido bajo la licencia MIT. Consulta [LICENSE](LICENSE).

---

*Este programa ha sido programado íntegramente con inteligencia artificial (IA).*