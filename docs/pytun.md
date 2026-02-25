h1. Pytun — InvGate Connector

{{toc}}

h2. ¿Qué problema resuelve?

InvGate necesita conectarse desde sus servidores en la nube a servicios que están dentro de la red privada del cliente (LDAP para autenticación, bases de datos, servidores de email, etc.).

El problema es que esos servicios están *detrás de un firewall*: el servidor de InvGate no puede iniciar una conexión directa hacia ellos, porque el firewall del cliente bloquea las conexiones entrantes.

!connector_flow_problem.png!

La solución habitual sería pedirle al cliente que abra puertos en su firewall, pero eso es costoso, requiere aprobaciones de seguridad y cada cliente lo configura distinto.

*El Connector resuelve esto al revés*: en lugar de que InvGate se conecte al cliente, es el cliente quien se conecta a InvGate. Una vez establecida esa conexión, el tráfico puede fluir en ambas direcciones, como si fuera una llamada telefónica: quien llama es el cliente, pero los dos pueden hablar.

!connector_flow_solution.png!

h2. Ventajas respecto a una VPN

* *Instalación simple*: instalador de Windows que no requiere conocimientos de networking especializados.
* *Resiliencia*: renegocia automáticamente la conexión y se recupera solo de interrupciones de red.
* *Independencia de topología*: funciona independientemente de la arquitectura de red del cliente.

h2. ¿Cómo funciona el túnel?

El Connector usa un mecanismo llamado *túnel SSH reverso*. SSH es un protocolo de red cifrado que los firewalls casi siempre permiten salir (porque se usa para administrar servidores remotos). El Connector aprovecha eso para crear un canal seguro.

El flujo completo, paso a paso:

# *El Connector inicia una conexión SSH* hacia el servidor cloud de InvGate (conexión saliente: el firewall la permite).
# A través de esa conexión, el Connector le pide al servidor cloud: _"abrí el puerto 15000 en tu lado, y todo lo que llegue ahí mandámelo a mí"_.
# El servidor cloud acepta y queda escuchando en ese puerto.
# Cuando una aplicación InvGate (por ejemplo, InvGate Service Management) necesita consultar el LDAP del cliente, envía la solicitud al servidor cloud en ese puerto.
# El servidor cloud recibe esa solicitud y la reenvía al Connector a través del túnel SSH.
# El Connector recibe los datos y los envía al servicio local (LDAP, base de datos, etc.) como si fuera una conexión local normal.
# La respuesta del servicio viaja de vuelta por el mismo camino.

Para el servicio local, parece que la consulta viene de adentro de la red. Para la aplicación InvGate, parece que el servicio está disponible directamente. *El Connector es el intermediario transparente*.

h3. Diagrama del flujo de datos

<pre>
  RED DEL CLIENTE (on-premises)             INVGATE CLOUD
  ─────────────────────────────             ──────────────────────

  ┌──────────────────────────────┐          ┌────────────────────┐
  │                              │          │                    │
  │  ┌──────────┐                │          │  ┌──────────────┐  │
  │  │ Servicio │                │          │  │  Aplicación  │  │
  │  │  local   │                │          │  │   InvGate    │  │
  │  │(LDAP/DB/ │                │          │  └──────┬───────┘  │
  │  │  SMTP)   │                │          │         │ consulta │
  │  └────▲─────┘                │          │         ▼          │
  │       │ conexión             │   SSH    │  ┌──────────────┐  │
  │       │ local                │          │  │   Servidor   │  │
  │  ┌────┴─────────────┐ Túnel  │          │  │    cloud     │  │
  │  │    Connector     │◄═══════╪══════════╪═►│  InvGate     │  │
  │  │     (pytun)      │ cifrado│          │  └──────────────┘  │
  │  └──────────────────┘        │          │                    │
  │                              │          └────────────────────┘
  │  Firewall: solo permite      │
  │  conexiones salientes ───────┼──────────►  (paso 1: el Connector llama)
  └──────────────────────────────┘

  Flujo de datos una vez establecido el túnel:
  Aplicación InvGate → Servidor cloud → [túnel SSH] → Connector → Servicio local
  Servicio local → Connector → [túnel SSH] → Servidor cloud → Aplicación InvGate
</pre>

h3. ¿Por qué SSH?

* SSH es un protocolo ampliamente permitido en los firewalls corporativos (puerto 22 saliente).
* Cifra todo el tráfico con *ECDSA 521*, sin necesidad de VPN. Incluye validación de identidad del servidor para prevenir ataques man-in-the-middle.
* Soporta nativamente el reenvío de puertos (port forwarding), que es la base del mecanismo.
* Es estable y confiable para conexiones de larga duración.

h3. ¿Qué pasa si el túnel se cae?

El Connector monitorea constantemente el estado de cada túnel. Si detecta que uno se cayó (por un corte de red, reinicio del servidor, timeout), lo reinicia automáticamente. Este ciclo de supervisión corre cada 30 segundos.

h2. Componentes principales

* *pytun (Connector)*: el ejecutable de Windows que corre en el servidor del cliente. Gestiona todos los túneles y los reinicia si fallan. Es agnóstico al protocolo del tráfico tunelado (LDAP, SMTP, etc.) — la autenticación a los servicios locales es responsabilidad de las aplicaciones que los consumen, no del Connector.
* *Servidor cloud (External Server)*: el servidor SSH de InvGate que acepta la conexión del Connector y expone los puertos para que las aplicaciones InvGate los usen.
* *Servidor de inspección*: el Connector expone una API local en el puerto 9999 que permite ver el estado de los túneles, logs y configuración (solo accesible desde localhost por razones de seguridad).

h2. Links

* "Repositorio":https://github.com/InvGate/pytun
* "Build producción (Jenkins)":https://ci.invgate.com/job/pytun-build/: genera el @.exe@ de producción usando PyInstaller
* "Build staging (Jenkins)":https://ci.invgate.com/job/pytun-build-staging/: igual pero para staging

h2. Cómo hacer deploy

Ejecutar el Jenkins job correspondiente al entorno:
* "Producción":https://ci.invgate.com/job/pytun-build/
* "Staging":https://ci.invgate.com/job/pytun-build-staging/

*Importante*: producción y staging usan claves RSA distintas (embebidas en el @.exe@ en tiempo de build). Un config de staging no va a funcionar con el @.exe@ de producción, y viceversa — el Connector simplemente no se autorizará sin un error claro en los logs.

h2. Requisitos del sistema

| Componente | Requerimiento |
| Sistema operativo | Windows Server 2012 R2 o superior |
| Procesador | Dual-core 2 GHz mínimo |
| RAM | 4 GB (agregar 1 GB por cada 150 conexiones simultáneas) |
| Almacenamiento | 200 MB + espacio para logs |
| Red | 20 Mbps mínimo |
| Dependencias | Microsoft Visual C++ Redistributable 2015-2022 |

h2. Setup de desarrollo

Requiere tener instalado Python (3.10+) y pip.

# Crear un virtualenv y activarlo
# Ejecutar @pip install -r requirements.txt@
# Agregar al root del proyecto: @connector.ini@ y @mac_address_pub_key@, y los configs de túneles en la carpeta @configs/@ (pedirlos a infra, hay un entorno de staging disponible)
# Iniciar el Connector con @python pytun.py@

*Importante*: ante cualquier cambio, actualizar la versión en "version.py":https://github.com/InvGate/pytun/blob/master/version.py siguiendo "semantic versioning":https://semver.org/

h2. Testing

Antes de publicar una versión, instalar el Connector en una máquina Windows y verificar:

# Ejecutar el script de tests (el shortcut que crea el instalador) y confirmar que los túneles funcionan
# Verificar que @127.0.0.1:9999/info@ y @127.0.0.1:9999/status@ retornan información correcta
# Verificar que los túneles se reinician automáticamente: buscar el PID del túnel en @main_connector.log@ y matarlo con @taskkill /F /PID numero_de_pid@ en la terminal de Windows. En menos de 30 segundos debe reiniciarse.

h3. Testing de alertas HTTP

Para probar que las alertas HTTP se envían correctamente:

# Levantar un servidor local para recibir las alertas: @python -m http.server@ (escucha en el puerto 8000)
# Modificar @connector.ini@ para agregar una firma inválida (para forzar un error) y configurar la URL de alerta:

<pre><code class="text">
signature=firma_invalida_para_forzar_error

http_url=http://127.0.0.1:8000
http_password=pass
http_user=root
</code></pre>

El servidor debería empezar a recibir POST requests con las alertas de fallo.
