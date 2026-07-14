import { buildApp } from './app';

// Local / Render / Docker entry point.
const app = buildApp();
const port = process.env.PORT ?? 4000;
app.listen(port, () => console.log(`API running on :${port}`));
