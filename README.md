# Servidor de inferencia de AUDITAXES

Servicio independiente para varios proyectos. Ofrece `translate`, `review`, `detect_language` y `proofread` con una cola SQLite y un solo trabajador. Los modelos se ejecutan en Ollama, configurado por tarea. No usa AutoML ni entrena modelos.

## Preparación en Windows

Requiere Python 3.11 o posterior, Ollama para Windows y un controlador NVIDIA compatible. Ollama se queda en `127.0.0.1:11434`; solo este gateway recibe solicitudes de la red privada. No publique el puerto 4110 en Internet: HTTP envía la clave y los textos sin cifrar.

```powershell
cd C:\Servicios\InferenciaAvanzado
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure claves únicas de al menos 24 caracteres en variables de entorno. Ejemplo de desarrollo:

```powershell
$env:INFERENCE_API_KEYS = '{"auditaxes":"cambia-esto-por-un-secreto-largo-y-unico"}'
$env:INFERENCE_DB = 'C:\Servicios\InferenciaAvanzado\data\jobs.sqlite3'
$env:FALLBACK_MODEL = 'qwen3:4b-instruct-2507-q4_K_M'
$env:PREFERRED_MODEL = 'qwen3:14b'
python -m uvicorn server:app --host 192.168.0.107 --port 4110 --workers 1
```

Descargue `ollama pull qwen3:4b-instruct-2507-q4_K_M` y `ollama pull qwen3:14b` en la workstation. El segundo es la opción potente propuesta para la 3090 (Q4_K_M, aproximadamente 9.3 GB de pesos); el primero es el fallback acordado (aproximadamente 2.5 GB). La calidad y la latencia para textos reales todavía deben medirse. La clave de ejemplo nunca debe utilizarse fuera de desarrollo. En la API de AUDITAXES configure `INFERENCE_URL=http://192.168.0.107:4110` e `INFERENCE_API_KEY` con el mismo secreto. El navegador no recibe esa clave.

## Traslado a la workstation

1. Compruebe que `192.168.0.107` es una IP fija o una reserva DHCP de la workstation. Instale Git, Python 3.11+ y Ollama para Windows; compruebe `python --version`, `ollama --version` y `nvidia-smi`. No se necesita instalar PyTorch ni volver a instalar CUDA Toolkit para esta aplicación.
2. Tras publicar el repositorio, ejecute `git clone https://github.com/suit-softwareMX/InferenciaAvanzado.git C:\Servicios\InferenciaAvanzado`, cree `.venv` y ejecute `pip install -r requirements.txt`. Ejecute `ollama pull qwen3:4b-instruct-2507-q4_K_M` y `ollama pull qwen3:14b`; verifique con `ollama list`. Mantenga Ollama en localhost.
3. Genere una clave aleatoria en la workstation con `[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))`. Guárdela fuera del repositorio y úsela en `INFERENCE_API_KEYS` como `{"auditaxes":"CLAVE"}`. Configure las demás variables del ejemplo en la sesión o en el gestor de secretos del servicio. No copie `.env`, la base SQLite ni los modelos al repositorio.
4. Abra solo TCP 4110 en el firewall de Windows para la IP de la máquina que ejecuta la API del editor. Ejemplo desde PowerShell elevado: `New-NetFirewallRule -DisplayName 'Inferencia AUDITAXES privada' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 4110 -LocalAddress 192.168.0.107 -RemoteAddress IP_DE_LA_API -Profile Private`. No abra 11434 ni cree una regla para cualquier origen.
5. Arranque **una sola instancia** con el comando Uvicorn anterior. Compruebe desde la máquina de la API `Invoke-RestMethod http://192.168.0.107:4110/healthz`: `database=true`, `ollama=true`, `models_ready=true` y `models.translate.active=qwen3:14b`. Pruebe `POST /v1/jobs` y `GET /v1/jobs/{id}` con una frase no sensible antes de usar contenido real.
6. En la máquina de AUDITAXES, establezca `INFERENCE_URL=http://192.168.0.107:4110` e `INFERENCE_API_KEY=CLAVE` en el entorno del proceso que arranca la API o `iniciar-todo.cmd`. El lanzador nunca inicia inferencia local y conserva esa clave entre arranques. Reinicie la API y pruebe traducción, corrección y revisión en el editor; ninguna acción de IA debe aprobar o publicar.
7. Para operación permanente, ejecute Uvicorn mediante un servicio Windows bajo una cuenta restringida, con reinicio automático y respaldo de `data/jobs.sqlite3` (incluidos sus archivos WAL, mediante copia consistente). Mantenga el directorio de datos accesible solo a la cuenta del servicio. Si la conexión sale de la LAN privada, añada VPN o TLS antes de enviar textos o claves.

## Contrato HTTP

`POST /v1/jobs` requiere `Authorization: Bearer <clave-del-proyecto>` y acepta:

```json
{
  "task": "translate",
  "input": {
    "source_locale": "es",
    "target_locale": "en",
    "fields": {"title": "Auditoría financiera"}
  }
}
```

La respuesta `202` contiene `{ "id": "...", "status": "queued" }`. Consulte `GET /v1/jobs/{id}` con la misma clave hasta obtener `succeeded` o `failed`. En `succeeded`, `result.fields` conserva exactamente los identificadores de entrada.

Para `review`, agregue `translated_fields` con los mismos identificadores. Su resultado es `{"issues":[{"field":"title","severity":"medium","category":"terminology","message":"..."}]}`; la lista puede estar vacía. La revisión de un modelo es una sugerencia editorial, no una aprobación.

`translate` acepta `source_locale`/`target_locale` `es`→`en` o `en`→`es`, y devuelve `{"fields":{"title":"..."}}`. `proofread` recibe `source_locale` (`es` o `en`) y `fields`, y devuelve los mismos identificadores con el texto completo corregido. `detect_language` recibe solo `fields` y devuelve `{"locale":"es"}`, `{"locale":"en"}` o `{"locale":"unknown"}`. La detección incierta debe confirmarse en el cliente. Las variables opcionales `DETECT_MODEL` y `PROOFREAD_MODEL` permiten usar otro modelo por tarea.

`GET /healthz` indica disponibilidad de base de datos, Ollama y `models_ready`. `models` informa por tarea `active`, `preferred_available` y `using_fallback`. Cada trabajo terminado incluye el modelo efectivamente usado. La elección ocurre en el servidor según los modelos instalados, nunca en el cliente; no descarga ni carga un modelo para consultar salud. Una petición rechazada por validación usa `422`, una cola llena `429`, y un trabajo ajeno responde `404`.

## Operación y límites

- Una instancia y un trabajador por archivo SQLite. Los trabajos `running` se reencolan al reiniciar. No inicie varios trabajadores Uvicorn contra la misma base.
- El lanzador local de `AuditaxesMainSite` inicia este servicio junto con la API y genera una clave temporal compartida; Ollama se instala y arranca por separado.
- Máximo 100 trabajos pendientes, cuerpo HTTP de 50 KB, 60 campos y 4000 caracteres de texto fuente por trabajo, y 60 segundos por llamada al modelo. Estos límites se pueden ajustar en `server.py` si la carga real lo requiere.
- SQLite contiene los textos de entrada y resultados; proteja y respalde `data/jobs.sqlite3`. Los logs no guardan los textos.
- Escuche en `127.0.0.1` durante desarrollo. En la 3090, ejecute Ollama localmente al servidor y acceda a este servicio mediante una red privada; configure `OLLAMA_URL`, las claves por proyecto y las rutas de datos en ese equipo.
- `PREFERRED_MODEL` configura el modelo potente y `FALLBACK_MODEL` el ligero. Se pueden sustituir por tarea con `TRANSLATE_PREFERRED_MODEL`, `REVIEW_PREFERRED_MODEL`, `PROOFREAD_PREFERRED_MODEL`, `DETECT_PREFERRED_MODEL` y sus variantes `_FALLBACK_MODEL`. Si el preferido no está instalado, usa el fallback sin cambiar endpoints.

## Pruebas sin modelo

Después del renderizado, `pip install -r requirements-dev.txt` y `python -m unittest test_server.py` ejecutan pruebas con un motor simulado; no llaman a Ollama. La integración de AUDITAXES también usa un proveedor simulado en sus pruebas.
