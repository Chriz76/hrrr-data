export default {
  async scheduled(event, env, ctx) {
    const owner = "Chriz76";
    const repo = "hrrr-data";
    const pipelineBranch = "tiles";
    const workflowFileName = "sync.yml";

    // Pull Zone der Bunny-Storage-Zone: index.json im Root zeigt auf den
    // aktuell live stehenden Run (runs/<run>/...).
    const pullZoneBase = "https://hrrr.b-cdn.net";

    const githubHeaders = {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${env.GH_PAT}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "Cloudflare-Worker"
    };

    // HRRR / Open-Meteo S3 Konfiguration
    // Der 15-Minuten-Datensatz endet immer bei +18 h (geprüft: +19 h = 404);
    // nur die 48-h-Zyklen (00/06/12/18Z) gehen im stündlichen Datensatz weiter.
    // Ein Bucket erscheint stündlich und ist ~1,5 h nach Run-Start vollständig.
    const modelName = "ncep_hrrr_conus_15min";
    const hoursToCheck = 6; // Suchfenster in Stunden (Lag ~1,5 h)
    const forecastOffsetHours = 18; // letzte Datei des Runs = +18 h (HORIZON_END_HOURS)

    // SCHRITT 1: Prüfen, ob die Pipeline bereits läuft oder wartet.
    // cancel-in-progress ist im Workflow false -> auch queued Runs blockieren.
    for (const status of ["in_progress", "queued"]) {
      const workflowUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFileName}/runs?status=${status}&branch=${pipelineBranch}`;
      const workflowCheck = await fetch(workflowUrl, { headers: githubHeaders });
      if (!workflowCheck.ok) {
        console.log(`⚠️ Workflow-Status '${status}' nicht abrufbar (${workflowCheck.status}). Abbruch.`);
        return;
      }
      const runData = await workflowCheck.json();
      if (runData.total_count > 0) {
        console.log(`Pipeline läuft bereits (${status}). Dieser Durchlauf wird übersprungen.`);
        return;
      }
    }

    // SCHRITT 2: Neuesten VOLLSTÄNDIGEN HRRR-Run (Open-Meteo S3) ermitteln
    // Auf die volle Stunde abrunden: die S3-Buckets sind stündlich (HH00Z).
    const utcNow = new Date(Math.floor(Date.now() / 3600000) * 3600000);
    let targetTime = null;
    let targetRunString = "";

    for (let i = 0; i < hoursToCheck; i++) {
      const checkTime = new Date(utcNow.getTime() - i * 60 * 60 * 1000);
      const YYYY = checkTime.getUTCFullYear();
      const MM = String(checkTime.getUTCMonth() + 1).padStart(2, '0');
      const DD = String(checkTime.getUTCDate()).padStart(2, '0');
      const HH = String(checkTime.getUTCHours()).padStart(2, '0');

      // Ordnerpfad auf S3 (Start des Runs)
      const runHourDir = `${HH}00Z`;
      const folderStr = `${YYYY}-${MM}-${DD}T${HH}:00`;

      // Ziel-Datei auf S3 ermitteln (Run-Startzeit + forecastOffsetHours)
      const s3TargetTime = new Date(checkTime.getTime() + forecastOffsetHours * 60 * 60 * 1000);
      const s3YYYY = s3TargetTime.getUTCFullYear();
      const s3MM = String(s3TargetTime.getUTCMonth() + 1).padStart(2, '0');
      const s3DD = String(s3TargetTime.getUTCDate()).padStart(2, '0');
      const s3HH = String(s3TargetTime.getUTCHours()).padStart(2, '0');

      // Dateiname der letzten erwarteten S3-Datei (z.B. 2026-08-06T0200.om)
      const isoFile = `${s3YYYY}-${s3MM}-${s3DD}T${s3HH}00.om`;
      const s3Url = `https://openmeteo.s3.amazonaws.com/data_spatial/${modelName}/${YYYY}/${MM}/${DD}/${runHourDir}/${isoFile}`;

      const headResp = await fetch(s3Url, { method: "HEAD" });
      if (headResp.status === 200) {
        targetTime = checkTime;
        targetRunString = folderStr;
        console.log(`🎯 Neuer vollständiger HRRR-Run (inkl. ${isoFile}) gefunden: ${targetRunString}Z`);
        break;
      }
    }

    if (!targetTime) {
      console.log("❌ Kein vollständiger HRRR-Datensatz im Abfragefenster gefunden.");
      return;
    }

    // SCHRITT 3: Prüfen, ob dieser Run schon live ist.
    // Die HRRR-Pipeline lädt die Kacheln direkt nach Bunny Storage und stellt am
    // Ende index.json im Root der Zone um. index.json.run ist die Run-ID des
    // aktuell live stehenden Runs (Format YYYYMMDD_HHMM, siehe pipeline.py).
    const rY = targetTime.getUTCFullYear();
    const rM = String(targetTime.getUTCMonth() + 1).padStart(2, '0');
    const rD = String(targetTime.getUTCDate()).padStart(2, '0');
    const rH = String(targetTime.getUTCHours()).padStart(2, '0');
    const rMi = String(targetTime.getUTCMinutes()).padStart(2, '0');
    const runId = `${rY}${rM}${rD}_${rH}${rMi}`;

    const indexUrl = `${pullZoneBase}/index.json`;
    console.log(`🔍 Prüfe Bunny-Index auf bereits veröffentlichten Run: ${runId}`);

    const indexResp = await fetch(indexUrl, { cache: "no-store" });
    if (indexResp.ok) {
      let liveRun = null;
      try {
        const indexData = await indexResp.json();
        liveRun = typeof indexData.run === "string" ? indexData.run : null;
      } catch (err) {
        console.log(`⚠️ index.json nicht lesbar (${err}). Weiter mit Dispatch.`);
      }

      // Run-IDs sind lexikografisch sortierbar (YYYYMMDD_HHMM).
      if (liveRun && liveRun >= runId) {
        console.log(`💾 Live ist bereits ${liveRun} (>= ${runId}). Lauf wird übersprungen.`);
        return;
      }
      console.log(`ℹ️ Live ist ${liveRun ?? "unbekannt"} -> Run ${runId} wird gebaut.`);
    } else if (indexResp.status === 404) {
      console.log("ℹ️ Noch keine index.json in der Zone -> Run wird gebaut.");
    } else {
      console.log(`⚠️ Unerwarteter Status von ${indexUrl}: ${indexResp.status}. Abbruch.`);
      return;
    }

    // SCHRITT 4: GitHub Action triggern
    console.log(`🚀 Trigger Workflow ${workflowFileName} auf Branch '${pipelineBranch}' für Run ${targetRunString}Z...`);
    const dispatchUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFileName}/dispatches`;

    const dispatchResponse = await fetch(dispatchUrl, {
      method: "POST",
      headers: githubHeaders,
      body: JSON.stringify({
        ref: pipelineBranch,
        inputs: {
          run: targetRunString
        }
      })
    });

    if (dispatchResponse.ok) {
      console.log("✅ GitHub Action erfolgreich gestartet.");
    } else {
      const errorText = await dispatchResponse.text();
      console.error(`❌ GitHub API Fehler (${dispatchResponse.status}):`, errorText);
    }   
  }
};