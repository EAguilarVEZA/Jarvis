// SuperPasos — native iOS/Android app (Expo / React Native)
// The native shell loads the COMPLETE web app (every feature: step counter, story,
// map, animals, parche, impacto nacional, mascot, wallet, barcode, recorded voice...)
// and adds the native superpowers a web page can't do on its own: the hardware step
// chip (background steps), real local notifications, and opening Maps natively.
import React, { useRef, useEffect } from 'react';
import { SafeAreaView, StyleSheet, StatusBar, ActivityIndicator, View, Text, AppState, Linking } from 'react-native';
import { WebView } from 'react-native-webview';
import * as Notifications from 'expo-notifications';
import { Pedometer } from 'expo-sensors';

const WEB_URL = 'https://superpodercamina.vercel.app/';

Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowAlert: true, shouldPlaySound: true, shouldSetBadge: false }),
});

// Injected into the web app so it can talk to the native shell.
const INJECTED = `
(function(){
  if (window.__spBridge) return;
  window.__spBridge = true;
  window.CYA_NATIVE = true;          // the web app checks this flag for native mode
  window.cyaNotify = function(payload){
    try { window.ReactNativeWebView.postMessage(JSON.stringify(payload)); } catch(e){}
  };
  // Lock the page like a native screen: no page scroll, no rubber-band/pull-to-refresh.
  // (Inner panels keep their own overflow-y:auto, so they still scroll internally.)
  var st = document.createElement('style');
  st.textContent = 'html,body{height:100%!important;max-height:100%!important;overflow:hidden!important;overscroll-behavior:none!important;}';
  document.head.appendChild(st);
})();
true;
`;

export default function App(){
  const webref = useRef(null);
  const walkStart = useRef(null);   // Date the current walk began
  const pedoSub = useRef(null);     // live watchStepCount subscription
  const liveBase = useRef(0);       // latest live step count from the watch

  useEffect(() => { Notifications.requestPermissionsAsync().catch(() => {}); }, []);

  // ---- Read the phone's hardware step chip (same source as Apple Health / Google Fit) ----
  function injectSteps(n){
    if (webref.current) webref.current.injectJavaScript(`window.cyaSetSteps && window.cyaSetSteps(${Math.round(n)});true;`);
  }
  async function startWalk(){
    try{
      const avail = await Pedometer.isAvailableAsync();
      if (!avail){ injectSteps(-1); return; }              // tell the web app to fall back
      try { await Pedometer.requestPermissionsAsync(); } catch(_){}
      walkStart.current = new Date();
      liveBase.current = 0;
      if (pedoSub.current){ pedoSub.current.remove(); pedoSub.current = null; }
      // Live updates while the app is in the foreground.
      pedoSub.current = Pedometer.watchStepCount(r => {
        if (r && typeof r.steps === 'number'){ liveBase.current = r.steps; injectSteps(r.steps); }
      });
    }catch(_){ injectSteps(-1); }
  }
  // When the app returns to the foreground, backfill steps the chip counted while the
  // screen was locked / app was backgrounded (iOS CMPedometer logs them continuously).
  async function backfillWalk(){
    if (!walkStart.current) return;
    try{
      const r = await Pedometer.getStepCountAsync(walkStart.current, new Date());
      if (r && typeof r.steps === 'number') injectSteps(Math.max(r.steps, liveBase.current));
    }catch(_){}
  }
  function stopWalk(){
    if (pedoSub.current){ pedoSub.current.remove(); pedoSub.current = null; }
    walkStart.current = null; liveBase.current = 0;
  }
  useEffect(() => {
    const sub = AppState.addEventListener('change', st => { if (st === 'active') backfillWalk(); });
    return () => { sub.remove(); stopWalk(); };
  }, []);

  // ---- Daily coupon-expiry reminders (local notifications) ----
  async function scheduleReminders(title){
    try{
      const t = 'SuperPasos';
      await Notifications.scheduleNotificationAsync({ content:{ title:t, body:`Guardamos tu cupón "${title}" en tu billetera. Te recordaremos antes de que venza.` }, trigger:{ seconds:10 } });
      for (let d=1; d<=6; d++){ const left = 7-d;
        await Notifications.scheduleNotificationAsync({ content:{ title:t, body:`Tu cupón "${title}" vence en ${left} día${left>1?'s':''}. ¡Úsalo!` }, trigger:{ seconds: d*86400 } }); }
      await Notifications.scheduleNotificationAsync({ content:{ title:'¡Vence mañana!', body:`Tu cupón "${title}" vence mañana — úsalo hoy.` }, trigger:{ seconds: 6*86400 } });
    }catch(_){}
  }

  function onMessage(e){
    try{
      const msg = JSON.parse(e.nativeEvent.data);
      if (!msg) return;
      if (msg.type === 'coupon_chosen') scheduleReminders(msg.title || 'tu cupón');
      else if (msg.type === 'start_walk') startWalk();
      else if (msg.type === 'stop_walk') stopWalk();
      else if (msg.type === 'open_maps' && msg.url) Linking.openURL(msg.url).catch(() => {});
    }catch(_){}
  }

  return (
    <SafeAreaView style={s.fill}>
      <StatusBar barStyle="light-content" backgroundColor="#070B16" />
      <WebView
        ref={webref}
        source={{ uri: WEB_URL }}
        injectedJavaScript={INJECTED}
        onMessage={onMessage}
        originWhitelist={['*']}
        javaScriptEnabled
        domStorageEnabled
        geolocationEnabled
        allowsInlineMediaPlayback
        mediaPlaybackRequiresUserAction={false}
        scrollEnabled={false}
        bounces={false}
        overScrollMode="never"
        showsVerticalScrollIndicator={false}
        showsHorizontalScrollIndicator={false}
        startInLoadingState
        renderLoading={() => (
          <View style={s.loading}>
            <Text style={s.brand}>SuperPasos</Text>
            <Text style={s.tag}>Tus pasos, su superpoder</Text>
            <ActivityIndicator size="large" color="#19E37D" style={{ marginTop: 18 }} />
          </View>
        )}
        setSupportMultipleWindows={false}
        onShouldStartLoadWithRequest={() => true}
      />
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  fill: { flex: 1, backgroundColor: '#070B16' },
  loading: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, justifyContent: 'center', alignItems: 'center', backgroundColor: '#070B16' },
  brand: { color: '#fff', fontSize: 30, fontWeight: '900', letterSpacing: -0.5 },
  tag: { color: '#19E37D', fontSize: 14, fontWeight: '700', marginTop: 4 },
});
