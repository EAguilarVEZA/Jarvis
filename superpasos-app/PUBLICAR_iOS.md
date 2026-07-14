# SuperPasos — Publicar en el App Store (iOS)

Guía paso a paso. Tú ya tienes: **cuenta Apple Developer** ✓, **Xcode** ✓, Mac.
Construimos en la **nube con EAS** (no necesitas compilar localmente).

App: hybrid Expo — un caparazón nativo que carga la app web completa
(`https://superpodercamina.vercel.app/`) y añade los poderes nativos:
podómetro por hardware (pasos en segundo plano), notificaciones locales y abrir Maps.

---

## 0) Una sola vez — preparar tu máquina
```bash
# Node 18+ y la CLI de EAS
npm install -g eas-cli

# Entra a tu cuenta de Expo (la misma del Snack)
eas login
```

## 1) El proyecto
Copia la carpeta `superpasos-app/` a tu Mac (contiene App.js, app.json, eas.json,
package.json y assets/). Luego:
```bash
cd superpasos-app
npm install
npx expo install --fix     # alinea versiones de dependencias al SDK
```

> Si quieres probarlo primero en tu teléfono con Expo Go: `npx expo start` y escanea el QR.

## 2) Conectar el proyecto a EAS
```bash
eas build:configure
```
Esto crea el `projectId` en `app.json` y prepara las credenciales. Cuando pregunte,
deja que EAS **genere y administre las credenciales de firma de iOS** automáticamente
(certificado + provisioning profile). Usa el Bundle ID **`com.superpasos.app`**.

## 3) Compilar el binario de iOS (en la nube)
```bash
eas build --platform ios --profile production
```
- Tarda ~10–20 min. Al final te da un enlace con el `.ipa` listo.
- (Opcional) Para probar antes en un simulador: `eas build -p ios --profile preview`.

## 4) Crear la app en App Store Connect
1. Entra a https://appstoreconnect.apple.com → **My Apps → +**.
2. Plataforma **iOS**, Nombre **SuperPasos**, idioma principal **Español (México/LatAm)**.
3. Bundle ID: **com.superpasos.app** (el mismo que en `app.json`).
4. SKU: `superpasos-001`. Guarda.
5. Copia el **App ID (ascAppId)** que aparece en la URL/Información de la app y, si vas
   a usar `eas submit` con el perfil, pégalo en `eas.json` (junto con tu Apple ID y Team ID).

## 5) Subir el build
```bash
eas submit --platform ios --profile production
```
EAS sube el `.ipa` a App Store Connect (te pedirá tu Apple ID / contraseña de app si
hace falta). En ~10–30 min el build aparece en la pestaña **TestFlight** y luego como
versión seleccionable.

> Recomendado: prueba primero en **TestFlight** (invítate a ti mismo y a tu esposa)
> antes de enviar a revisión pública.

## 6) Completar la ficha (App Information / Version)
- **Subtítulo:** Tus pasos, su superpoder.
- **Descripción:** Camina y convierte tus pasos en comidas para niños en Colombia.
  Empresas patrocinan, los bancos de alimentos entregan, y tú ganas cupones. Cada
  caminata alimenta. Incluye retos, parche (grupo), impacto nacional y cupones.
- **Palabras clave:** caminar, pasos, donar, comida, niños, Colombia, salud, cupones, impacto.
- **URL de soporte:** https://superpasos.com
- **URL de marketing:** https://superpasos.com
- **Política de privacidad (obligatoria):** publica una página (ej. https://superpasos.com/privacidad)
  — puedo generártela.
- **Categoría:** Salud y forma física (primaria) · Estilo de vida (secundaria).
- **Capturas (obligatorias):** sube pantallas 6.7" y 6.5" (puedes tomarlas desde el
  simulador de Xcode o tu iPhone). Mínimo 3.

## 7) Privacidad de la app (App Privacy)
Responde el cuestionario con honestidad. Recolectamos:
- **Ubicación (precisa):** para verificar la caminata y mostrar tiendas. Vinculada al usuario.
- **Identificadores / datos de uso:** para el funcionamiento y métricas agregadas.
- **Salud y forma física (pasos):** se leen del sensor; se usan para activar donaciones.
Marca "los datos NO se usan para rastreo de terceros" (no usamos ad tracking).

## 8) Notas para el revisor (importante — evita rechazo 4.2)
En "App Review Information → Notes" escribe algo como:
> SuperPasos no es solo un sitio web: usa el sensor de movimiento del iPhone
> (CMPedometer) para contar pasos en segundo plano, envía notificaciones locales de
> recordatorio de cupones, y usa GPS para verificar la caminata y abrir Maps. La app
> funciona en modo demostración sin necesidad de crear cuenta (pueden completar una
> caminata simulada con el botón "¿Sin sensor? Tocar para simular").

Da una **cuenta demo** o indica el modo demo para que el revisor pruebe sin registrarse.

## 9) Enviar a revisión
Selecciona el build de TestFlight como la versión, marca "Manualmente" o "Automático"
para la publicación, y pulsa **Add for Review → Submit**. Apple suele responder en 1–3 días.

---

## Cada vez que actualices la app web
La mayoría de cambios viven en la web (`superpodercamina.vercel.app`) y se reflejan
**al instante** en la app sin re-enviar a Apple. Solo necesitas un build nuevo cuando
cambies algo **nativo** (íconos, permisos, notificaciones, versión). En ese caso:
sube `version`/`buildNumber` en `app.json`, `eas build`, `eas submit`.

## Pendientes que puedo hacer por ti
- Generar la **página de Política de Privacidad** (requisito de Apple).
- Generar las **capturas de pantalla** para la ficha.
- Configurar también **Android / Google Play** cuando quieras.
