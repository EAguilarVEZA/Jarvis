# Camina y Alimenta — Custom App Build & App Store Publishing Guide

_Last updated: June 2026_

This is the roadmap to take Camina y Alimenta from the Expo Go preview to a real,
installable app on the **Apple App Store** and **Google Play Store**, with the full
native powers: always‑on background step counting and geofence "you're near a store
where you can redeem" alerts.

There are two phases:

- **Phase 1 — Custom build (EAS):** turn the project into a standalone app binary that
  includes the native modules Expo Go can't run.
- **Phase 2 — Store publishing:** put that binary on the App Store and Google Play.

---

## Phase 1 — The custom build (what unlocks the new features)

Today the app runs inside **Expo Go** (the scan‑a‑QR sandbox). Expo Go can't do
background location or always‑on background step counting. A **custom build** —
produced by Expo's cloud service **EAS Build** — bundles those native capabilities
into your own app binary (`.ipa` for iPhone, `.aab`/`.apk` for Android).

**What the custom build adds:**

- **Geofence redeem alerts** — the app watches your location in the background and
  sends a notification when you pass near a store that matches a coupon in your wallet
  ("Estás cerca de Carulla — ¿quieres redimir tu cupón?"). Uses `expo-location`
  geofencing + `expo-task-manager`.
- **Always‑on background step counting** — steps keep counting on Android even with
  the app fully closed (iPhone already backfills via the motion chip; this makes
  Android match it). Uses a background task / foreground service.
- Everything you already have (hardware pedometer, daily reminders, wallet, barcode)
  carries over unchanged.

**Who does what in Phase 1:**

| Step | Who | Notes |
|---|---|---|
| Add geofence + background modules and config | **Me** | Code + `app.json` permissions + `eas.json` |
| Draft the privacy policy (required by both stores) | **Me** | You review and host it |
| Create a free **Expo account** | **You** | expo.dev — needed to run EAS builds |
| Run the first EAS build | **You or me with your login** | `eas build` — runs in Expo's cloud (~10–20 min) |
| Install the build on your phone and test | **You** | Via QR / TestFlight / direct `.apk` |

> Android can be built and tested **without any paid account** — you can sideload the
> `.apk` directly. iPhone test installs need either the $99 Apple account (for
> TestFlight) or a free 7‑day dev provisioning.

---

## Accounts & costs — set these up first

| Account | Cost | Why | When |
|---|---|---|---|
| **Expo account** | Free | Runs EAS builds & submissions | Now |
| **Apple Developer Program** | **$99 / year** | Required to publish to the App Store / TestFlight | Before iOS launch |
| **Google Play Console** | **$25 one‑time** | Required to publish to Google Play | Before Android launch |

**Individual vs. Organization — recommendation: register as an Organization.**

- It shows your brand (e.g. "Camina y Alimenta" / your foundation) as the seller
  instead of your personal name.
- **It exempts you from Google's new 12–20 tester / 14‑day closed‑testing requirement**
  that applies to *personal* accounts created after Nov 13, 2023.
- Both stores require a **D‑U‑N‑S number** for organizations — it's **free** from
  Dun & Bradstreet and can take 1–2 weeks, so request it early. (If Camina y Alimenta
  is or will be a registered nonprofit/foundation, use that entity; Apple also offers
  **fee waivers** for nonprofits, schools, and government bodies.)

---

## Phase 2A — Publishing to the Apple App Store

1. **Enroll** in the Apple Developer Program ($99/yr) at developer.apple.com using an
   Apple Account with two‑factor on and your **legal name** (or org + D‑U‑N‑S).
2. **Create the app record** in **App Store Connect** (name, bundle ID, language).
3. **Upload the build** with `eas submit` (Expo pushes the `.ipa` to App Store Connect).
4. **TestFlight** — invite yourself/testers to try the real build before going public.
5. **Fill the store listing:** screenshots (per device size), description, keywords,
   support URL, **privacy policy URL**, and the **App Privacy** labels (you'll declare
   that the app uses **Location** and **Motion/Fitness** data and how).
6. **Submit for review.** First reviews typically take ~24–48 hours.
7. **Release** — automatically or on a date you choose.

---

## Phase 2B — Publishing to Google Play

1. **Register** at the Google Play Console ($25 one‑time); complete identity
   verification. Register as an **Organization** to skip the closed‑testing mandate.
2. **Create the app** and complete the dashboard tasks.
3. **Upload the build** (`.aab`) with `eas submit`.
4. **Complete the required declarations:** store listing (icon, screenshots,
   descriptions), **content rating** questionnaire, **Data Safety** form (declare
   Location + Activity/steps data), target audience, and **privacy policy URL**.
5. **(Personal accounts only)** run the 14‑day closed test with the required testers.
   Organization accounts skip this.
6. **Submit for review** and roll out (you can stage to a % of users first). First
   review can take a few days.

---

## Assets I'll need from you (or can help create)

- **App icon / logo** — a high‑res square (1024×1024). If you have the mascot/brand
  art, send it; otherwise I can generate options.
- **App name & subtitle** — e.g. "Camina y Alimenta — Tus pasos, su superpoder".
- **Store description** — short + long. I can draft both in Spanish.
- **Screenshots** — I can capture these from the running app for each required size.
- **Privacy policy** — required by both stores (you use location + motion data). I'll
  draft it; you host it on a simple page (we already deploy web pages on Vercel).
- **Support email / URL.**

---

## A note on app‑store acceptance (so there are no surprises)

Apple and Google can reject apps that are *only* a wrapped website. Camina y Alimenta
is **not** that — it has substantial native functionality (hardware step counting,
background geofencing, local notifications, the scannable barcode wallet). Those native
features are exactly what make it a legitimate app in both stores' eyes. We lead with
them in the listing.

---

## How to "request deployment" when it's ready — your checklist

When the build is tested and the listings are filled, **you** trigger the go‑live.
Concretely, when you're ready, do these (I'll prepare everything up to each step):

1. ✅ Expo account created.
2. ✅ Apple Developer ($99/yr) **and/or** Google Play ($25) account active (Org + D‑U‑N‑S
   if going the organization route).
3. ✅ Privacy policy reviewed and hosted.
4. ✅ Build installed on your phone and tested (pedometer, geofence alert, wallet).
5. ▶️ **Tell me "submit to TestFlight / Play internal testing"** — I prepare the build and
   the `eas submit` config; you run the submit with your store login (I never handle
   your store passwords).
6. ▶️ After your own testing, **press "Submit for Review"** in App Store Connect / Play
   Console. That final review submission is the one action you do yourself.

I handle all the code, configuration, assets, and walk you through each store screen.
The only things that must be **you** are: paying the fees, signing in to your store
accounts, and clicking the final "Submit for Review."

---

## Suggested order of operations

1. **Now:** I add the geofence + background‑step code and build config; I draft the
   privacy policy. You create a free Expo account and (if going Org) request a D‑U‑N‑S
   number.
2. **This week:** first EAS build → you install and test on your phone.
3. **When happy:** enroll in the store(s), I prepare listings + screenshots, you submit
   for review.
4. **Launch.** 🎉
