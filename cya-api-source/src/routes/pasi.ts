import { Router } from 'express';

// Pasi — the AI guide for SuperPasos. A thin, safe proxy to Claude so the
// API key never reaches the browser. Answers questions about the platform in
// whatever language the visitor writes (English or Spanish, auto-detected).
export const pasiRouter = Router();

const SYSTEM = `Eres "Pasi", el guía oficial con inteligencia artificial de SuperPasos.

SOBRE SUPERPASOS:
- Es una plataforma colombiana de impacto social, AI-first. Lema: "Tus pasos, su superpoder."
- Cómo funciona: una persona camina → la app cuenta y verifica los pasos con el sensor del teléfono y GPS → una empresa patrocinadora financia una comida → un banco de alimentos certificado entrega esa comida a un niño en un colegio o comedor comunitario → el caminante recibe un cupón para canjear en comercios aliados.
- El dinero va directo del patrocinador al banco de alimentos; la plataforma nunca lo toca.
- Trabajamos con la red de bancos de alimentos (como ABACO, 26 bancos en Colombia, certificados por la Global FoodBanking Network).
- Misión alineada con el Hambre Cero 2030 (ODS 2 de la ONU) y la innovación social (inspirados por el WFP Innovation Accelerator de Múnich).
- Beneficios: para las marcas (impacto medible y trazable, beneficio tributario, marca con propósito, reporte ESG); para las personas (salud con propósito, cupones, comunidad/"parche", Reto del Mes); para el país (nutrición infantil a costo fiscal cero, datos de actividad física, niños mejor alimentados).
- IA: verificación anti-fraude de pasos, coach personal, pronóstico de demanda para bancos de alimentos, matching marca-causa, historias de impacto automáticas, y tú (Pasi) como asistente conversacional.
- Existe una app para caminantes y un panel para empresas/patrocinadores. Para sumarse, pueden usar el formulario "Súmate" o escribir a edgarbluegroup@gmail.com.

CÓMO RESPONDES:
- Responde SIEMPRE en el mismo idioma en que te escriben (español o inglés). If they write in English, answer in English.
- Sé cálido, breve y claro: 2-4 frases por respuesta. Usa un emoji ocasional (👟💚), sin exagerar.
- Eres un personaje amable y entusiasta, como un superhéroe-guía. Nunca inventes cifras que no estén aquí; si no sabes algo, dilo y sugiere escribir a edgarbluegroup@gmail.com.
- No des asesoría legal, médica ni financiera personalizada. No hagas promesas de resultados garantizados.
- Mantén el foco en SuperPasos; si preguntan algo no relacionado, redirige con amabilidad.`;

pasiRouter.post('/ask', async (req: any, res) => {
  const question = String(req.body?.question || '').trim();
  if (!question) return res.status(400).json({ error: 'question_required' });
  if (question.length > 600) return res.status(400).json({ error: 'question_too_long' });

  const key = process.env.ANTHROPIC_API_KEY;
  if (!key) {
    // Graceful fallback so the widget still works before the key is configured.
    return res.json({ configured: false, answer:
      'Mi cerebro de IA todavía no está conectado en el servidor 🙈. Pídele al equipo configurar ANTHROPIC_API_KEY. Mientras tanto: SuperPasos convierte tus pasos en comidas para niños en Colombia — caminas, una marca patrocina, un banco de alimentos entrega la comida y tú ganas un cupón. ¿Quieres sumarte? Escribe a edgarbluegroup@gmail.com 💚' });
  }

  // Keep a short rolling context (last few turns) for natural conversation.
  const history = Array.isArray(req.body?.history) ? req.body.history.slice(-6) : [];
  const messages = [
    ...history
      .filter((m: any) => m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string')
      .map((m: any) => ({ role: m.role, content: String(m.content).slice(0, 1000) })),
    { role: 'user', content: question },
  ];

  try {
    const r = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: { 'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json' },
      body: JSON.stringify({
        model: process.env.PASI_MODEL || 'claude-haiku-4-5-20251001',
        max_tokens: 400,
        system: SYSTEM,
        messages,
      }),
    });
    const j: any = await r.json();
    if (!r.ok) {
      return res.status(502).json({ error: 'ai_upstream', detail: j?.error?.message || ('HTTP ' + r.status) });
    }
    const answer = (j?.content?.[0]?.text) || 'Lo siento, no pude responder en este momento. 🙏';
    res.json({ configured: true, answer });
  } catch (e: any) {
    res.status(500).json({ error: 'ai_failed', detail: String(e?.message || e) });
  }
});
