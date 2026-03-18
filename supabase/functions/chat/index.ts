import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { encodeBase64 } from "jsr:@std/encoding/base64";
import { JWT } from "npm:google-auth-library@9.7.0";

// ═══════════════════════════════════════════════════════════════
// TYPES
// ═══════════════════════════════════════════════════════════════

interface BookingState {
  step: number;
  retries: number;
  total_retries: number;
  reason: string | null;
  date: string | null;
  time: string | null;
  doctor_id: string | null;
  patient_data: Record<string, string | null>;
  booking_data: Record<string, string | null>;
  availability_error: string | null;
  alternative_time: string | null;
  suggested_slots: SlotInfo[];
  patient_id: string | null;
  saved: boolean;
  is_modification: boolean;
}

interface SlotInfo {
  date: string;
  time: string;
  doctor_id: string;
  day_name: string;
  readable_date: string;
  day_offset: number;
  distance: number;
}

interface Doctor {
  id: string;
  name: string;
  specialty: string;
  schedule: Record<string, string>;
}

interface ServiceMapping {
  specialty: string;
  keywords: string[];
}

interface PatientField {
  key: string;
  label: string;
  type: string;
  required: boolean;
  unique_match: boolean;
  extraction_hint: string;
}

interface ClinicConfig {
  clinic: {
    name: string;
    phone: string;
    address: string;
    timezone: string;
    assistant_name: string;
    specialty_label: string;
    working_hours: Record<string, string>;
    parking: boolean;
    insurance: string[];
    assistant_voice?: string;
    assistant_voice_rate?: string;
  };
  services: ServiceMapping[];
  booking: {
    slot_duration_minutes: number;
    patient_fields: PatientField[];
  };
  llm: {
    model: string;
    base_url: string;
    max_tokens_extract: number;
    max_tokens_respond: number;
    temperature_extract: number;
    temperature_respond: number;
  };
}

interface ChatRequest {
  conversation_id?: string;
  message: string;
  clinic_id?: string;
}

interface ChatResponse {
  response: string;
  conversation_id: string;
  step: number;
  audio?: string;
}

// ═══════════════════════════════════════════════════════════════
// TTS LOGIC (Azure Cognitive Services Speech)
// ═══════════════════════════════════════════════════════════════

async function getAzureTTS(
  text: string,
  azureKey: string,
  azureRegion: string,
  voice = "en-US-AriaNeural",
  rate = "+0%"
): Promise<string> {
  if (!text || !azureKey || !azureRegion) {
    console.error("TTS: Missing text, key, or region");
    return "";
  }

  const escapedText = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");

  const ssml = `<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='en-US'>` +
    `<voice name='${voice}'>` +
    `<prosody rate='${rate}'>${escapedText}</prosody>` +
    `</voice></speak>`;

  const url = `https://${azureRegion}.tts.speech.microsoft.com/cognitiveservices/v1`;

  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Ocp-Apim-Subscription-Key": azureKey,
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
        "User-Agent": "ClinicAI-TTS",
      },
      body: ssml,
    });

    if (!resp.ok) {
      console.error(`TTS: Azure returned ${resp.status}: ${await resp.text()}`);
      return "";
    }

    const audioBuffer = await resp.arrayBuffer();
    if (audioBuffer.byteLength === 0) {
      console.error("TTS: Azure returned empty audio");
      return "";
    }

    const b64 = encodeBase64(new Uint8Array(audioBuffer));
    console.error(`TTS: Generated ${b64.length} chars of base64 audio`);
    return b64;
  } catch (e) {
    console.error("TTS: Azure fetch error:", e);
    return "";
  }
}

// ═══════════════════════════════════════════════════════════════
// TIME & DATE UTILITIES
// ═══════════════════════════════════════════════════════════════

function getCurrentTime(timezone: string): Date {
  const now = new Date();
  try {
    const str = now.toLocaleString('en-US', { timeZone: timezone });
    return new Date(str);
  } catch {
    return now;
  }
}

function parseTime12h(timeStr: string): { hour: number; minute: number } | null {
  if (!timeStr) return null;
  let s = timeStr.trim().toLowerCase();
  if (s === 'noon') return { hour: 12, minute: 0 };
  if (s === 'midnight') return { hour: 0, minute: 0 };

  let m = s.match(/^(\d{1,2}):(\d{2})\s*(am|pm)$/i);
  if (m) {
    let h = parseInt(m[1]);
    const min = parseInt(m[2]);
    const ampm = m[3].toLowerCase();
    if (ampm === 'pm' && h !== 12) h += 12;
    if (ampm === 'am' && h === 12) h = 0;
    return { hour: h, minute: min };
  }

  m = s.match(/^(\d{1,2})\s*(am|pm)$/i);
  if (m) {
    let h = parseInt(m[1]);
    const ampm = m[2].toLowerCase();
    if (ampm === 'pm' && h !== 12) h += 12;
    if (ampm === 'am' && h === 12) h = 0;
    return { hour: h, minute: 0 };
  }

  m = s.match(/^(\d{1,2}):(\d{2})$/);
  if (m) {
    let h = parseInt(m[1]);
    const min = parseInt(m[2]);
    if (!s.includes('am') && !s.includes('pm') && h >= 1 && h <= 7) h += 12;
    return { hour: h, minute: min };
  }

  m = s.match(/^(\d{1,2})$/);
  if (m) {
    let h = parseInt(m[1]);
    if (h >= 1 && h <= 7) h += 12;
    return { hour: h, minute: 0 };
  }

  return null;
}

function timeToMinutes(t: { hour: number; minute: number }): number {
  return t.hour * 60 + t.minute;
}

function formatTime(t: { hour: number; minute: number }): string {
  const h = t.hour % 12 || 12;
  const ampm = t.hour >= 12 ? 'PM' : 'AM';
  return `${h}:${String(t.minute).padStart(2, '0')} ${ampm}`;
}

function normalizeDate(dateStr: string, now: Date): string {
  if (!dateStr) return dateStr;
  const d = dateStr.replace(/(\d+)(st|nd|rd|th)/gi, '$1').replace(/,/g, '').trim();
  const dl = d.toLowerCase();

  if (['today', "today's"].includes(dl)) return fmtDate(now);
  if (['tomorrow', 'tmrw', 'tmr'].includes(dl)) return fmtDate(addDays(now, 1));
  if (dl.includes('day after tomorrow')) return fmtDate(addDays(now, 2));

  if (/^\d{4}-\d{2}-\d{2}$/.test(d)) return d;

  const monthNames: Record<string, number> = {
    january: 0, february: 1, march: 2, april: 3, may: 4, june: 5,
    july: 6, august: 7, september: 8, october: 9, november: 10, december: 11,
    jan: 0, feb: 1, mar: 2, apr: 3, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11
  };

  for (const [name, monthIdx] of Object.entries(monthNames)) {
    const re = new RegExp(`${name}\\s+(\\d{1,2})`, 'i');
    const re2 = new RegExp(`(\\d{1,2})\\s+${name}`, 'i');
    let match = dl.match(re) || dl.match(re2);
    if (match) {
      const day = parseInt(match[1]);
      let dt = new Date(now.getFullYear(), monthIdx, day);
      if (dt < now) dt = new Date(now.getFullYear() + 1, monthIdx, day);
      return fmtDate(dt);
    }
  }

  const days = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
  for (let i = 0; i < days.length; i++) {
    if (dl.includes(days[i])) {
      const current = now.getDay();
      let ahead = i - current;
      if (ahead <= 0) ahead += 7;
      if (dl.includes('next')) ahead += 7;
      return fmtDate(addDays(now, ahead));
    }
  }

  return dateStr;
}

function fmtDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}

function getReadableDate(dateStr: string | null): string {
  if (!dateStr) return '';
  try {
    const dt = new Date(dateStr + 'T00:00:00');
    return dt.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
  } catch { return dateStr; }
}

function isVagueTime(t: string | null): boolean {
  if (!t) return false;
  return ['morning', 'afternoon', 'evening'].includes(t.trim().toLowerCase());
}

// ═══════════════════════════════════════════════════════════════
// DATA LAYER
// ═══════════════════════════════════════════════════════════════

async function getSheetsToken(): Promise<string> {
  const credsStr = Deno.env.get('GOOGLE_SERVICE_ACCOUNT');
  if (!credsStr) {
    console.error("Missing GOOGLE_SERVICE_ACCOUNT");
    throw new Error("Missing Google Service Account config");
  }
  const creds = JSON.parse(credsStr);
  const client = new JWT({
    email: creds.client_email,
    key: creds.private_key,
    scopes: ['https://www.googleapis.com/auth/spreadsheets'],
  });
  const res = await client.authorize();
  return res.access_token!;
}

async function loadDoctors(sheetId: string, token: string): Promise<Doctor[]> {
  try {
    const url = `https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Doctors!A2:J`;
    const resp = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (!resp.ok) {
      console.error("Sheets error loading doctors:", await resp.text());
      return [];
    }
    const json = await resp.json();
    const rows = json.values || [];
    
    return rows.map((r: any[]) => {
      const schedule: Record<string, string> = {};
      const d = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
      for (let i = 0; i < 7; i++) {
        if (r[i + 3] && r[i + 3].trim() !== '') {
          schedule[d[i]] = r[i + 3].trim();
        }
      }
      return {
        id: String(r[0]),
        name: r[1],
        specialty: (r[2] || 'general').toLowerCase(),
        schedule
      };
    });
  } catch(e) {
    console.error("Load doctors fail:", e);
    return [];
  }
}

async function loadBookings(sheetId: string, token: string): Promise<any[]> {
  try {
    const [bResp, pResp] = await Promise.all([
      fetch(`https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Bookings!A2:F`, { headers: { Authorization: `Bearer ${token}` } }),
      fetch(`https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Patients!A2:D`, { headers: { Authorization: `Bearer ${token}` } })
    ]);
    const bJson = await bResp.json();
    const pJson = await pResp.json();
    
    const pRows = pJson.values || [];
    const patientsMap: Record<string, any> = {};
    for (const p of pRows) {
       patientsMap[String(p[0])] = { name: p[1], phone: p[2], email: p[3] };
    }

    const bRows = bJson.values || [];
    return bRows.map((r: any[]) => {
       const pid = String(r[4]);
       const pData = patientsMap[pid] || {};
       return {
         date: r[0],
         start_time: r[1],
         end_time: r[2],
         doctor_id: r[3],
         patient_id: pid,
         patient_name: pData.name || '',
         patient_phone: pData.phone || '',
         patient_email: pData.email || '',
         reason: r[5] || ''
       };
    });
  } catch(e) {
    console.error("Load bookings fail:", e);
    return [];
  }
}

async function saveBooking(sheetId: string, state: BookingState, slotDuration: number) {
  try {
    const token = await getSheetsToken();
    const startT = parseTime12h(state.time!);
    let endTimeStr = state.time!;
    if (startT) {
      const endMin = timeToMinutes(startT) + slotDuration;
      endTimeStr = formatTime({ hour: Math.floor(endMin / 60), minute: endMin % 60 });
    }

    // Load Patients to check if patient exists by phone or email
    const pResp = await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Patients!A2:D`, { headers: { Authorization: `Bearer ${token}` } });
    const pJson = await pResp.json();
    const pRows = pJson.values || [];
    
    let patientId = '';
    const phone = (state.patient_data.phone || '').replace(/\D/g, '');
    const email = (state.patient_data.email || '').toLowerCase();
    
    for (const p of pRows) {
        const cPhone = (p[2] || '').replace(/\D/g, '');
        const cEmail = (p[3] || '').toLowerCase();
        if ((phone && cPhone === phone) || (email && cEmail === email)) {
            patientId = p[0];
            break;
        }
    }
    
    // Append to Patients if new
    if (!patientId) {
        let maxId = 0;
        for (const p of pRows) {
            const pid = parseInt(p[0] || '0', 10);
            if (!isNaN(pid) && pid > maxId) maxId = pid;
        }
        patientId = String(maxId + 1);
        const pValues = [[patientId, state.patient_data.name || '', state.patient_data.phone || '', state.patient_data.email || '']];
        await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Patients!A:D:append?valueInputOption=USER_ENTERED`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
          body: JSON.stringify({ values: pValues })
        });
    }

    // Fetch current bookings, append new, sort, and overwrite
    const bResp = await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Bookings!A2:F`, { headers: { Authorization: `Bearer ${token}` } });
    const bJson = await bResp.json();
    const bRows = bJson.values || [];
    
    bRows.push([
        state.date, 
        state.time, 
        endTimeStr, 
        state.doctor_id, 
        patientId,
        state.reason || ''
    ]);

    bRows.sort((a: any[], b: any[]) => {
        if (a[0] !== b[0]) return String(a[0]).localeCompare(String(b[0]));
        const ta = parseTime12h(a[1]);
        const tb = parseTime12h(b[1]);
        const ma = ta ? timeToMinutes(ta) : 0;
        const mb = tb ? timeToMinutes(tb) : 0;
        return ma - mb;
    });

    await fetch(`https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Bookings!A2:F?valueInputOption=USER_ENTERED`, {
      method: 'PUT',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: bRows })
    });
  } catch(e) {
    console.error('Save booking fail:', e);
  }
}

// ═══════════════════════════════════════════════════════════════
// AVAILABILITY ENGINE
// ═══════════════════════════════════════════════════════════════

function findMatchingDoctors(reason: string, doctors: Doctor[], services: ServiceMapping[]): string[] {
  const rl = (reason || '').toLowerCase();
  const matched: string[] = [];
  for (const doc of doctors) {
    const svc = services.find(s => s.specialty === doc.specialty);
    if (svc && svc.keywords.some(kw => rl.includes(kw))) matched.push(doc.id);
  }
  if (matched.length === 0 && doctors.length > 0) {
    for (const doc of doctors) {
      if (doc.specialty === 'general') matched.push(doc.id);
    }
    if (matched.length === 0) matched.push(doctors[0].id);
  }
  return matched;
}

function getDoctorHours(doc: Doctor, dayName: string): [{ hour: number; minute: number }, { hour: number; minute: number }] | null {
  const h = doc.schedule[dayName.toLowerCase()];
  if (!h) return null;
  const parts = h.split(' - ');
  if (parts.length !== 2) return null;
  const s = parseTime12h(parts[0]);
  const e = parseTime12h(parts[1]);
  if (!s || !e) return null;
  return [s, e];
}

function checkOverlap(
  bookings: any[], dateStr: string, startTime: string, endTime: string, doctorId: string
): boolean {
  const newStart = parseTime12h(startTime);
  const newEnd = parseTime12h(endTime);
  if (!newStart || !newEnd) return true;
  const nsm = timeToMinutes(newStart);
  const nem = timeToMinutes(newEnd);

  for (const b of bookings) {
    if (b.date !== dateStr || String(b.doctor_id) !== doctorId) continue;
    const bs = parseTime12h(b.start_time);
    const be = parseTime12h(b.end_time);
    if (!bs || !be) continue;
    const bsm = timeToMinutes(bs);
    const bem = timeToMinutes(be);
    if (nsm < bem && bsm < nem) return true;
  }
  return false;
}

function findNearestSlots(
  reason: string, doctors: Doctor[], services: ServiceMapping[], bookings: any[],
  workingHours: Record<string, string>, slotDuration: number, now: Date,
  preferredDate?: string | null, preferredTime?: string | null, count = 3,
  filterStart?: { hour: number; minute: number } | null, filterEnd?: { hour: number; minute: number } | null
): SlotInfo[] {
  const matched = findMatchingDoctors(reason, doctors, services);
  const allDocIds = doctors.map(d => d.id);
  const docsToTry = [...matched, ...allDocIds.filter(d => !matched.includes(d))];

  let anchorDate: Date;
  if (preferredDate) {
    const nd = normalizeDate(preferredDate, now);
    try { anchorDate = new Date(nd + 'T00:00:00'); } catch { anchorDate = now; }
    if (fmtDate(anchorDate) < fmtDate(now)) return [];
  } else {
    anchorDate = now;
  }

  let anchorTime: { hour: number; minute: number } | null = null;
  if (preferredTime && preferredTime !== 'REJECTED' && !isVagueTime(preferredTime)) {
    anchorTime = parseTime12h(preferredTime);
  }
  if (!anchorTime) {
    anchorTime = fmtDate(anchorDate) === fmtDate(now)
      ? { hour: now.getHours() + 1, minute: 0 }
      : { hour: 9, minute: 0 };
  }
  const anchorMinutes = timeToMinutes(anchorTime);
  const slots: SlotInfo[] = [];

  for (let dayOff = 0; dayOff < 15; dayOff++) {
    const checkDate = addDays(anchorDate, dayOff);
    if (fmtDate(checkDate) < fmtDate(now)) continue;
    const dayName = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'][checkDate.getDay()];
    if (!workingHours[dayName]) continue;

    const chParts = workingHours[dayName].split(' - ');
    if (chParts.length !== 2) continue;
    const clinicStart = parseTime12h(chParts[0]);
    const clinicEnd = parseTime12h(chParts[1]);
    if (!clinicStart || !clinicEnd) continue;

    const dateStr = fmtDate(checkDate);
    const foundTimes = new Set<string>();

    for (const docId of docsToTry) {
      const doc = doctors.find(d => d.id === docId);
      if (!doc) continue;
      const docH = getDoctorHours(doc, dayName);
      if (!docH) continue;

      const effStart = timeToMinutes(docH[0]) > timeToMinutes(clinicStart) ? docH[0] : clinicStart;
      const effEnd = timeToMinutes(docH[1]) < timeToMinutes(clinicEnd) ? docH[1] : clinicEnd;
      if (timeToMinutes(effStart) >= timeToMinutes(effEnd)) continue;

      let currMin = timeToMinutes(effStart);
      const endMin = timeToMinutes(effEnd);

      if (fmtDate(checkDate) === fmtDate(now)) {
        const nowMin = (now.getHours() + 1) * 60;
        if (nowMin > currMin) currMin = nowMin;
      }

      while (currMin + slotDuration <= endMin) {
        const slotTime = { hour: Math.floor(currMin / 60), minute: currMin % 60 };
        const slotStr = formatTime(slotTime);

        if (filterStart && timeToMinutes(slotTime) < timeToMinutes(filterStart)) { currMin += slotDuration; continue; }
        if (filterEnd && timeToMinutes(slotTime) >= timeToMinutes(filterEnd)) break;
        if (foundTimes.has(slotStr)) { currMin += slotDuration; continue; }

        const endTimeStr = formatTime({ hour: Math.floor((currMin + slotDuration) / 60), minute: (currMin + slotDuration) % 60 });
        const overlap = checkOverlap(bookings, dateStr, slotStr, endTimeStr, docId);

        if (!overlap) {
          foundTimes.add(slotStr);
          const distance = dayOff === 0 ? Math.abs(currMin - anchorMinutes) : Infinity;
          slots.push({
            day_offset: dayOff, distance, time: slotStr, date: dateStr,
            doctor_id: docId,
            day_name: ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][checkDate.getDay()],
            readable_date: getReadableDate(dateStr),
          });
        }
        currMin += slotDuration;
      }
    }
    if (dayOff > 0 && slots.length >= count) break;
  }

  const sameDay = slots.filter(s => s.day_offset === 0).sort((a, b) => a.distance - b.distance);
  const future = slots.filter(s => s.day_offset > 0);
  return [...sameDay, ...future].slice(0, count);
}

function formatSlots(slots: SlotInfo[]): string {
  return slots.map(s => `${s.day_name} ${s.readable_date} at ${s.time}`).join(', ');
}

function isValidReason(reason: string | null, services: ServiceMapping[]): boolean {
  if (!reason) return false;
  const rl = reason.toLowerCase().trim();
  const generic = ['appointment', 'booking', 'schedule', 'help', 'hi', 'hey', 'hello', 'book', 'visit'];
  if (generic.includes(rl)) return false;
  const allKw = new Set<string>();
  for (const svc of services) svc.keywords.forEach(k => allKw.add(k));
  return Array.from(allKw).some(kw => rl.includes(kw));
}

// ═══════════════════════════════════════════════════════════════
// LLM CALLS
// ═══════════════════════════════════════════════════════════════

async function callLLM(
  baseUrl: string, model: string, apiKey: string,
  messages: any[], maxTokens: number, temperature: number, jsonMode = false
): Promise<string> {
  const body: any = { model, messages, max_tokens: maxTokens, temperature };
  if (jsonMode) body.response_format = { type: 'json_object' };

  const resp = await fetch(`${baseUrl}/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  return data.choices?.[0]?.message?.content?.trim() || '';
}

async function extractBookingInfo(
  cfg: ClinicConfig, apiKey: string, chatHistory: any[], text: string, state: BookingState, now: Date
): Promise<Record<string, any>> {
  const currentDate = now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  const currentTime = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });

  const pfSchema = cfg.booking.patient_fields.map(f =>
    `- '${f.key}': string | null — ${f.extraction_hint}. NULL if not given.`
  ).join('\n');
  const pfTemplate = [
    '"reason": string|null',
    '"date": "YYYY-MM-DD"|null',
    '"time": "HH:MM AM/PM"|null',
    ...cfg.booking.patient_fields.map(f => `"${f.key}": string|null`),
    '"is_modification": false',
  ].join(', ');

  const svcExamples = cfg.services.flatMap(s => s.keywords.slice(0, 3)).slice(0, 6).join(', ');

  const sysPrompt = `You are a JSON data extractor for a ${cfg.clinic.specialty_label} clinic.\nToday is ${currentDate}. Current time: ${currentTime}. Year: ${now.getFullYear()}.\n\nRules:\n- Resolve relative dates ("tomorrow", "Friday" = next upcoming).\n- If user agrees to a suggested time, extract that date+time.\n- If user says a number "1", "2", "3" after Sarah listed slots, pick that slot.\n- Extract ALL fields from a single message.\n- 'is_modification': true ONLY if user wants to cancel/reschedule an EXISTING appointment.\n\nCurrent state: step=${state.step}, reason=${state.reason || 'null'}, date=${state.date || 'null'}, time=${state.time || 'null'}\n\nFields:\n- 'reason': ${cfg.clinic.specialty_label} issue (e.g., ${svcExamples}). NULL if not specific.\n- 'date': YYYY-MM-DD. "REJECTED" if user rejects. NULL if unclear.\n- 'time': HH:MM AM/PM. For "morning"/"afternoon"/"evening" return that word. "REJECTED" if rejected. NULL if unclear.\n${pfSchema}\n- 'is_modification': boolean\n\nReturn ONLY valid JSON: {${pfTemplate}}`;

  const historySimple = chatHistory.filter((m: any) => m.role !== 'system').map((m: any) => ({ role: m.role, content: m.content }));

  try {
    const raw = await callLLM(cfg.llm.base_url, cfg.llm.model, apiKey, [
      { role: 'system', content: sysPrompt },
      { role: 'user', content: `History: ${JSON.stringify(historySimple)}\nUser Text: ${text}\nCurrent State: ${JSON.stringify({ step: state.step, reason: state.reason, date: state.date, time: state.time })}` },
    ], cfg.llm.max_tokens_extract, cfg.llm.temperature_extract, true);
    return JSON.parse(raw);
  } catch (e) {
    console.error('Extraction error:', e);
    return {};
  }
}

const FORBIDDEN_PHRASES = [
  "hold on", "let me check", "let me see", "wait while", "one moment", 
  "checking our schedule", "let me look", "pulling up", "i told you", 
  "as i said", "listen to me", "previously mentioned", "like i said"
];

function scrubResponse(text: string, doctorNames: string[]): string {
  let result = text;
  for (const name of doctorNames) {
    const regex = new RegExp(name.replace(/[-[\]{}()*+?.,\\^$|#\s]/g, '\\$&'), 'gi');
    result = result.replace(regex, "our dentist");
  }
  for (const phrase of [/(doctor|doc|patient)\s*(id)?\s*\d/gi]) {
    result = result.replace(phrase, "our dentist");
  }
  for (const phrase of FORBIDDEN_PHRASES) {
    const regex = new RegExp(phrase, 'gi');
    result = result.replace(regex, "");
  }
  return result.replace(/\*[^*]+\*/g, "").trim();
}

function validateResponse(text: string, step: number): { valid: boolean; issues: string[] } {
  const issues: string[] = [];
  if (!text || text.trim().length < 5) issues.push("Empty or too short");
  if (/\*[^*]+\*/.test(text)) issues.push("Contains asterisk action");
  if ([1,2,3,4].includes(step) && !text.includes("?")) issues.push("No question mark");
  for (const phrase of FORBIDDEN_PHRASES) {
    if (text.toLowerCase().includes(phrase)) issues.push(`Contains forbidden phrase: ${phrase}`);
  }
  if (step !== 5) {
    const sentenceCount = (text.match(/[.!?]+/g) || []).length;
    if (sentenceCount > 3) issues.push("Too verbose");
  }
  return { valid: issues.length === 0, issues };
}

function fallbackExtractDateTime(text: string): { date: string | null; time: string | null } {
  const tl = text.toLowerCase().trim();
  let date: string | null = null;
  let time: string | null = null;
  if (tl.includes('tomorrow') || tl.includes('tmrw')) date = 'tomorrow';
  else if (tl.includes('today')) date = 'today';
  else if (tl.includes('day after tomorrow')) date = 'day after tomorrow';
  else {
    const days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];
    for (const d of days) {
      if (tl.includes(d)) { date = (tl.includes('next') ? 'next ' : '') + d; break; }
    }
  }
  if (/mornin/.test(tl)) time = 'morning';
  else if (/afternoo/.test(tl)) time = 'afternoon';
  else if (/evenin/.test(tl)) time = 'evening';
  else {
    const tm = tl.match(/(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b/);
    if (tm) time = `${tm[1]}:${tm[2] || '00'} ${tm[3].toUpperCase()}`;
    else {
      const am = tl.match(/\bat\s+(\d{1,2})(?::(\d{2}))?\b/);
      if (am) time = `${parseInt(am[1])}:${am[2] || '00'} ${parseInt(am[1]) >= 1 && parseInt(am[1]) <= 7 ? 'PM' : 'AM'}`;
    }
  }
  return { date, time };
}

async function generateResponse(
  cfg: ClinicConfig, apiKey: string, chatHistory: any[], state: BookingState, now: Date, doctors: Doctor[], bookings: any[]
): Promise<string> {
  const doctorNames = doctors.map(d => d.name);
  const rd = getReadableDate(state.date);
  const currentDate = now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  const currentTime = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
  const c = cfg.clinic;

  const parking = c.parking ? 'Free parking available.' : 'No dedicated parking.';
  const insurance = c.insurance.length ? c.insurance.join(', ') : 'Not accepted directly.';
  const fieldLabels = cfg.booking.patient_fields.filter(f => f.required).map(f => f.label);
  const fieldStr = fieldLabels.length <= 2 ? fieldLabels.join(' and ') : fieldLabels.slice(0, -1).join(', ') + ', and ' + fieldLabels[fieldLabels.length - 1];
  const svcExamples = cfg.services.flatMap(s => s.keywords.slice(0, 3)).slice(0, 6).join(', ');

  let instruction = '';
  if (state.is_modification) {
    instruction = `The patient wants to change/cancel an existing appointment. Tell them to call ${c.phone}. Ask if they want to book NEW instead. End with ?`;
  } else if (state.step === 1) {
    instruction = `Welcome patient to ${c.name}. Ask what ${c.specialty_label} issue they need help with (e.g., ${svcExamples}). End with ?`;
  } else if (state.step === 2) {
    let hint = '';
    let slots: SlotInfo[] = [];
    const wh = c.working_hours;
    const dur = cfg.booking.slot_duration_minutes;
    
    if (!state.date && !state.time) {
      slots = findNearestSlots(state.reason || '', doctors, cfg.services, bookings, wh, dur, now);
    } else if (state.date && !state.time) {
      slots = findNearestSlots(state.reason || '', doctors, cfg.services, bookings, wh, dur, now, state.date);
    } else if (state.date && isVagueTime(state.time)) {
      const ranges = { morning: [{ hour: 8, minute: 0 }, { hour: 12, minute: 0 }], afternoon: [{ hour: 12, minute: 0 }, { hour: 17, minute: 0 }], evening: [{ hour: 17, minute: 0 }, { hour: 20, minute: 0 }] };
      const [vStart, vEnd] = (ranges as any)[(state.time||'').toLowerCase()] || [null, null];
      slots = findNearestSlots(state.reason || '', doctors, cfg.services, bookings, wh, dur, now, state.date, null, 3, vStart, vEnd);
    } else {
      slots = state.suggested_slots || [];
    }

    state.suggested_slots = slots;

    if (slots && slots.length > 0) {
      hint = `\nHINT: Suggest EXACTLY these available times: ${formatSlots(slots)}. NEVER invent any other times! Offer these choices to the patient!`;
    } else if (state.date) {
      hint = `\nHINT: We have NO availability near ${getReadableDate(state.date)}. Tell them we don't have availability on that date and ask if they'd like to try another day.`;
    }
    
    instruction = `Patient needs: '${state.reason}'. ${state.date ? 'They prefer ' + getReadableDate(state.date) + '.' : ''} You MUST follow the HINT below. If no HINT, ask what date and time work best. End with ?${hint}`;
  } else if (state.step === 3) {
    instruction = `Tell patient EXACTLY: "${state.availability_error}". End with ?`;
  } else if (state.step === 4) {
    instruction = `${rd} at ${state.time} IS AVAILABLE. Confirm and ask for ${fieldStr}. End with ?`;
  } else if (state.step === 5) {
    instruction = `BOOKING CONFIRMED. Say: "Your appointment is confirmed for ${rd} at ${state.time} for ${state.reason}. Thank you for choosing ${c.name}!" No questions.`;
  }

  const sysPrompt = `You are ${c.assistant_name}, receptionist at ${c.name}. Phone call.\nToday: ${currentDate}. Time: ${currentTime}.\nParking: ${parking}. Insurance: ${insurance}. Address: ${c.address}.\nRules: Be concise (1-2 sentences). Never mention doctor names/IDs. Never say "hold on" or "let me check". Never use asterisks.\n\nCURRENT INSTRUCTION: ${instruction}`;

  const msgs = [{ role: 'system', content: sysPrompt }, ...chatHistory.filter((m: any) => m.role !== 'system')];

  for (let attempt = 0; attempt < 5; attempt++) {
    try {
      const raw = await callLLM(cfg.llm.base_url, cfg.llm.model, apiKey, msgs, cfg.llm.max_tokens_respond, cfg.llm.temperature_respond);
      let cleaned = scrubResponse(raw, doctorNames);
      const { valid, issues } = validateResponse(cleaned, state.step);
      
      if (valid) return cleaned;
      
      console.warn(`[Gen Attempt ${attempt+1}] Invalid: ${issues.join(', ')} | Raw: ${cleaned}`);
      if (issues.some(i => i.includes("question mark")) && [1,2,3,4].includes(state.step)) {
        if (state.step === 1) cleaned = cleaned.replace(/[.!]+$/, '') + ` — what ${c.specialty_label} issue can I help with?`;
        else if (state.step === 2) cleaned = cleaned.replace(/[.!]+$/, '') + " — what date and time work best for you?";
        else if (state.step === 3) cleaned = cleaned.replace(/[.!]+$/, '') + " — would that alternative time work for you?";
        else if (state.step === 4) cleaned = cleaned.replace(/[.!]+$/, '') + ` — could I get your ${fieldStr}?`;
        if (validateResponse(cleaned, state.step).valid) return cleaned;
      }
    } catch (e) {
      console.error(`[Gen Attempt ${attempt+1}] Error:`, e);
    }
  }

  // Fallbacks
  if (state.is_modification) return `I can only book new appointments. Call us at ${c.phone} for changes. Would you like to book a new appointment instead?`;
  if (state.step === 3) return `I'm sorry, that time isn't available. ${state.availability_error}`;
  if (state.step === 4) return `Great, ${rd} at ${state.time} is available! Could I get your ${fieldStr} to finalize?`;
  if (state.step === 5) return `Your appointment is confirmed for ${rd} at ${state.time} for ${state.reason}. Thank you for choosing ${c.name}!`;
  return `I'm here to help book your ${c.specialty_label} appointment. What can I assist with?`;
}

// ═══════════════════════════════════════════════════════════════
// BOOKING STATE MACHINE
// ═══════════════════════════════════════════════════════════════

function createInitialState(cfg: ClinicConfig): BookingState {
  const pd: Record<string, string | null> = {};
  for (const f of cfg.booking.patient_fields) pd[f.key] = null;
  return {
    step: 1, retries: 0, total_retries: 0,
    reason: null, date: null, time: null, doctor_id: null,
    patient_data: pd, booking_data: {},
    availability_error: null, alternative_time: null,
    suggested_slots: [], patient_id: null, saved: false, is_modification: false,
  };
}

async function processMessage(
  text: string, state: BookingState, chatHistory: any[],
  cfg: ClinicConfig, apiKey: string, doctors: Doctor[], bookings: any[], now: Date
): Promise<string> {
  const services = cfg.services;
  const wh = cfg.clinic.working_hours;
  const slotDur = cfg.booking.slot_duration_minutes;

  const partial = await extractBookingInfo(cfg, apiKey, chatHistory, text, state, now);
  
  const fb = fallbackExtractDateTime(text);
  if (fb.date) {
    if (!partial.date || fb.date !== normalizeDate(partial.date, now)) {
      console.log(`[FALLBACK] Date override: ${partial.date} -> ${fb.date}`);
      partial.date = fb.date;
    }
  }
  if (fb.time && !partial.time) {
    console.log(`[FALLBACK] Time extraction: ${fb.time}`);
    partial.time = fb.time;
  }

  const oldStep = state.step;

  if (partial.reason && isValidReason(partial.reason, services)) state.reason = partial.reason;
  for (const f of cfg.booking.patient_fields) {
    if (partial[f.key]) state.patient_data[f.key] = partial[f.key];
  }
  if (partial.date === 'REJECTED') { state.date = null; state.step = 2; }
  else if (partial.date) state.date = normalizeDate(partial.date, now);
  if (partial.time === 'REJECTED') { state.time = null; state.step = 2; }
  else if (partial.time) state.time = partial.time;

  if (partial.is_modification && state.step <= 1) {
    const cancel = ['cancel', 'reschedule', 'modify'].some(w => text.toLowerCase().includes(w));
    state.is_modification = cancel;
  }

  if (!state.is_modification) {
    for (let loop = 0; loop < 5; loop++) {
      const prev = state.step;

      if (state.step === 1 && state.reason && isValidReason(state.reason, services)) {
        state.step = 2; state.retries = 0;
        state.suggested_slots = findNearestSlots(state.reason, doctors, services, bookings, wh, slotDur, now);
      } else if (state.step === 2 && state.date && state.time) {
        if (isVagueTime(state.time)) {
          const ranges = { morning: [{ hour: 8, minute: 0 }, { hour: 12, minute: 0 }], afternoon: [{ hour: 12, minute: 0 }, { hour: 17, minute: 0 }], evening: [{ hour: 17, minute: 0 }, { hour: 20, minute: 0 }] };
          const [vStart, vEnd] = (ranges as any)[state.time.toLowerCase()] || [null, null];
          const slots = findNearestSlots(state.reason || '', doctors, services, bookings, wh, slotDur, now, state.date, state.time, 3, vStart, vEnd);
          state.suggested_slots = slots;
          if (!slots.length) {
            state.availability_error = `No availability for ${state.time} on ${getReadableDate(state.date)}. Could you suggest another day?`;
            state.step = 3; state.retries = 0;
          } else {
            // Stay at step 2, but provide hints
            break;
          }
        } else {
          const slots = findNearestSlots(state.reason || '', doctors, services, bookings, wh, slotDur, now, state.date, state.time, 3);
          state.suggested_slots = slots;
          if (!slots.length) {
            state.availability_error = `No availability near ${getReadableDate(state.date)}. Could you suggest another day?`;
            state.step = 3; state.retries = 0;
          } else {
            const reqT = parseTime12h(state.time!);
            const matchSlot = slots.find(s => s.date === normalizeDate(state.date!, now) && reqT && timeToMinutes(parseTime12h(s.time)!) === timeToMinutes(reqT));
            if (matchSlot) {
              state.doctor_id = matchSlot.doctor_id;
              state.step = 4; state.retries = 0;
            } else {
              state.availability_error = `That slot isn't available. Nearest options: ${formatSlots(slots)}. Which works?`;
              state.step = 3; state.retries = 0;
            }
          }
        }
      } else if (state.step === 3) {
        const agree = ['yes', 'sure', 'ok', 'okay', 'fine', 'works', 'perfect', 'sounds good', 'great', 'yeah', 'yep', 'first'].some(w => text.toLowerCase().includes(w));
        if (agree && !partial.time && state.suggested_slots.length) {
          state.date = state.suggested_slots[0].date;
          state.time = state.suggested_slots[0].time;
        }
        if (state.date && state.time && !isVagueTime(state.time)) {
          state.step = 2; state.retries = 0; continue;
        }
      } else if (state.step === 4) {
        const allFilled = cfg.booking.patient_fields.filter(f => f.required).every(f => state.patient_data[f.key]);
        if (allFilled) {
          state.step = 5; state.retries = 0;
        }
      }

      if (state.step === prev) break;
    }
  }

  if (state.step === 5 && !state.saved) {
    state.saved = true;
  }

  chatHistory.push({ role: 'user', content: text });

  let response: string;
  const rd = getReadableDate(state.date);
  const c = cfg.clinic;
  const fieldLabels = cfg.booking.patient_fields.filter(f => f.required).map(f => f.label);
  const fieldStr = fieldLabels.length <= 2 ? fieldLabels.join(' and ') : fieldLabels.slice(0, -1).join(', ') + ', and ' + fieldLabels[fieldLabels.length - 1];

  if (state.step === 1 && chatHistory.filter((m: any) => m.role === 'user').length === 1 && text.trim().toLowerCase() === 'hello') {
    response = `Hello! I'm ${c.assistant_name}, the AI receptionist at ${c.name}. How can I help you today?`;
  } else if (state.step === 3 && state.availability_error) {
    response = state.availability_error;
  } else if (state.step === 4 && oldStep !== 4) {
    let dayName = '';
    try {
      const dt = new Date(normalizeDate(state.date!, now) + 'T00:00:00');
      dayName = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][dt.getDay()];
    } catch {}
    response = `${dayName} ${rd} at ${state.time} is available! Could I get your ${fieldStr} to book this?`;
  } else if (state.step === 5) {
    response = `Your appointment is confirmed for ${rd} at ${state.time} for ${state.reason}. Thank you for choosing ${c.name}!`;
  } else {
    response = await generateResponse(cfg, apiKey, chatHistory, state, now, doctors, bookings);
  }

  chatHistory.push({ role: 'assistant', content: response });
  return response;
}

// ═══════════════════════════════════════════════════════════════
// MAIN HANDLER
// ═══════════════════════════════════════════════════════════════

Deno.serve(async (req: Request) => {
  if (req.method === 'OPTIONS') {
    return new Response(null, {
      headers: {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization',
      },
    });
  }

  if (req.method !== 'POST') {
    return new Response(JSON.stringify({ error: 'Method not allowed' }), { status: 405, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
  }

  try {
    const body: ChatRequest = await req.json();
    const { message, clinic_id = 'default' } = body;
    let { conversation_id } = body;

    if (!message) {
      return new Response(JSON.stringify({ error: 'message is required' }), { status: 400, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
    }

    const supabaseUrl = Deno.env.get('SUPABASE_URL')!;
    const supabaseKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;
    const llmApiKey = Deno.env.get('LLM_API_KEY')!;
    const supabase = createClient(supabaseUrl, supabaseKey);

    // Parallelize Supabase config and conversation loading
    const [cfgRes, convRes] = await Promise.all([
      supabase.from('clinic_config').select('config, sheet_id').eq('id', clinic_id).single(),
      conversation_id ? supabase.from('conversations').select('*').eq('id', conversation_id).single() : Promise.resolve({ data: null })
    ]);

    const { data: cfgRow } = cfgRes;
    if (!cfgRow) {
      return new Response(JSON.stringify({ error: 'Clinic not found' }), { status: 404, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
    }
    const cfg: ClinicConfig = cfgRow.config;
    const sheet_id = cfgRow.sheet_id;
    if (!sheet_id) {
      return new Response(JSON.stringify({ error: 'Clinic setup incomplete: no Google Sheet assigned' }), { status: 500, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' } });
    }
    const now = getCurrentTime(cfg.clinic.timezone);

    let state: BookingState;
    let chatHistory: any[];

    if (convRes.data) {
      state = convRes.data.state;
      chatHistory = convRes.data.chat_history;
    } else {
      state = createInitialState(cfg);
      chatHistory = [];
      conversation_id = undefined;
    }

    // Parallelize Google Sheets data loading
    const sheetsToken = await getSheetsToken();
    const [doctors, bookings] = await Promise.all([
      loadDoctors(sheet_id, sheetsToken),
      loadBookings(sheet_id, sheetsToken)
    ]);

    const response = await processMessage(message, state, chatHistory, cfg, llmApiKey, doctors, bookings, now);

    if (state.step === 5 && state.saved) {
      try {
        await saveBooking(sheet_id, state, cfg.booking.slot_duration_minutes);
      } catch (e) {
        console.error('Save booking error:', e);
      }
    }

    if (conversation_id) {
      await supabase.from('conversations').update({ state, chat_history: chatHistory, updated_at: new Date().toISOString() }).eq('id', conversation_id);
    } else {
      const { data: newConv } = await supabase.from('conversations')
        .insert({ clinic_id, state, chat_history: chatHistory })
        .select('id').single();
      conversation_id = newConv?.id;
    }

    // Generate TTS via Azure Speech
    let audioBase64 = "";
    const azureKey = Deno.env.get('AZURE_SPEECH_KEY') || "";
    const azureRegion = Deno.env.get('AZURE_SPEECH_REGION') || "eastus";
    if (azureKey) {
      const voiceName = cfg.clinic.assistant_voice || "en-US-AriaNeural";
      const voiceRate = cfg.clinic.assistant_voice_rate || "+0%";
      audioBase64 = await getAzureTTS(response, azureKey, azureRegion, voiceName, voiceRate);
    }

    return new Response(JSON.stringify({
      response,
      conversation_id,
      step: state.step,
      audio: audioBase64,
    }), {
      headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
    });

  } catch (e) {
    console.error('Chat error:', e);
    return new Response(JSON.stringify({ error: 'Internal server error', details: String(e) }), {
      status: 500, headers: { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' },
    });
  }
});
