// WhooshCoach Client Application
let calendar = null;
let currentEventData = null;
let libraryDebounceTimer = null;

document.addEventListener('DOMContentLoaded', async () => {
  lucide.createIcons();
  initCalendar();
  await loadSettings();
  await loadStats();
  fetchLibraryWorkouts();
});

// ----------------- CALENDAR INITIALIZATION -----------------

function initCalendar() {
  const calendarEl = document.getElementById('calendar');
  calendar = new FullCalendar.Calendar(calendarEl, {
    initialView: 'dayGridMonth',
    headerToolbar: {
      left: 'prev,next today',
      center: 'title',
      right: 'dayGridMonth,timeGridWeek,listWeek'
    },
    height: 'auto',
    aspectRatio: 1.5,
    events: '/api/calendar',
    eventClick: function(info) {
      openEventDetails(info.event);
    },
    dateClick: function(info) {
      // Click any day on the calendar to add a Race, Workout, or Rest day
      openAddEventModal(info.dateStr, 'race');
    }
  });
  calendar.render();
}

// ----------------- STATS & DASHBOARD -----------------

async function loadStats() {
  try {
    const res = await fetch('/api/stats');
    if (!res.ok) return;
    const stats = await res.json();

    document.getElementById('completedTss').innerText = stats.completed_tss || 0;
    document.getElementById('plannedTss').innerText = stats.planned_tss || 0;
    document.getElementById('completedHours').innerText = stats.completed_hours || 0;
    document.getElementById('completedRides').innerText = stats.completed_rides || 0;

    // TSS percentage
    const pct = stats.planned_tss > 0 ? Math.min(100, Math.round((stats.completed_tss / stats.planned_tss) * 100)) : 0;
    document.getElementById('tssProgressBar').style.width = `${pct}%`;
    if (stats.coaching_mode === 'autonomous') {
      document.getElementById('weekTssBadge').innerText = stats.planned_tss > 0 ? `${pct}% • Autonomous AI` : 'Autonomous AI';
    } else {
      document.getElementById('weekTssBadge').innerText = `${pct}% of Plan`;
    }

    // Today's highlight
    const today = stats.today_workout;
    if (today) {
      document.getElementById('todayTitle').innerText = today.title;
      document.getElementById('todayZoneBadge').innerText = today.primary_zone || 'Z2 - Endurance';
      document.getElementById('todayStatusBadge').innerText = today.status.toUpperCase();
      document.getElementById('todayDuration').innerText = `${today.planned_duration_minutes} min`;
      document.getElementById('todayTss').innerText = today.planned_tss;

      if (today.workout && today.workout.description) {
        document.getElementById('todayDescription').innerText = today.workout.description;
      } else if (today.coach_notes) {
        document.getElementById('todayDescription').innerText = today.coach_notes;
      } else {
        document.getElementById('todayDescription').innerText = "Focus on steady pacing and fueling well.";
      }

      if (today.workout && today.workout.url) {
        const mwBtn = document.getElementById('btnTodayMyWhoosh');
        mwBtn.href = today.workout.url;
        mwBtn.classList.remove('hidden');
      }
      
      currentEventData = today;
    } else {
      document.getElementById('todayTitle').innerText = "No session scheduled for today";
      document.getElementById('todayDescription').innerText = "Take a rest day, or use the AI Plan button to generate your schedule.";
      document.getElementById('todayZoneBadge').innerText = "Rest Day";
      document.getElementById('todayStatusBadge').innerText = "REST";
      document.getElementById('todayDuration').innerText = "0 min";
      document.getElementById('todayTss').innerText = "0";
      document.getElementById('btnTodayMyWhoosh').classList.add('hidden');
    }
  } catch (e) {
    console.error("Failed to load dashboard stats:", e);
  }
}

// ----------------- EVENT DETAILS MODAL -----------------

async function openEventDetails(eventObj) {
  const props = eventObj.extendedProps;
  currentEventData = { id: props.db_id, ...props };

  document.getElementById('modalTitle').innerText = props.workout_title || eventObj.title;
  document.getElementById('modalDate').innerText = `Date: ${eventObj.startStr}`;
  document.getElementById('modalZoneTag').innerText = props.primary_zone || 'Z2';
  document.getElementById('modalStatusTag').innerText = (props.status || 'scheduled').toUpperCase();

  document.getElementById('modalDuration').innerText = `${props.planned_duration_minutes || 0} min`;
  document.getElementById('modalTss').innerText = props.planned_tss || 0;
  document.getElementById('modalActualTss').innerText = props.actual_tss != null ? `${props.actual_tss} TSS` : '--';

  if (props.actual_avg_watts || props.actual_avg_hr) {
    document.getElementById('modalActualPower').innerText = `${props.actual_avg_watts ? Math.round(props.actual_avg_watts) + 'W' : ''} ${props.actual_avg_hr ? Math.round(props.actual_avg_hr) + ' bpm' : ''}`.trim();
  } else {
    document.getElementById('modalActualPower').innerText = '--';
  }

  // Coach notes
  if (props.coach_notes) {
    document.getElementById('modalCoachNotes').innerText = props.coach_notes;
    document.getElementById('modalCoachNotesBox').classList.remove('hidden');
  } else {
    document.getElementById('modalCoachNotesBox').classList.add('hidden');
  }

  if (props.event_type === 'race') {
    const prio = props.race_priority || 'A';
    document.getElementById('modalZoneTag').innerText = `🏆 ${prio}-Race`;
    document.getElementById('modalDescription').innerText = props.athlete_notes || "Scheduled target race.";
    document.getElementById('modalSteps').innerText = `Discipline: ${props.race_type || 'Road Race'}\nTarget Distance: ${props.target_distance_km ? props.target_distance_km + ' km' : 'Not specified'}\nPlanned Duration: ${props.planned_duration_minutes || 0} mins\nTarget TSS: ${props.planned_tss || 0}\nStrategy Notes: ${props.athlete_notes || 'None'}`;
    document.getElementById('modalMyWhooshLink').classList.add('hidden');
  } else if (props.workout_id) {
    try {
      const res = await fetch(`/api/workouts/${props.workout_id}`);
      if (res.ok) {
        const w = await res.json();
        document.getElementById('modalDescription').innerText = w.description || "No description provided.";
        document.getElementById('modalSteps').innerText = w.workout_steps || "Open ride / Free ride.";
        if (w.url) {
          const mwLink = document.getElementById('modalMyWhooshLink');
          mwLink.href = w.url;
          mwLink.classList.remove('hidden');
        }
      }
    } catch (e) {
      console.error(e);
    }
  } else {
    document.getElementById('modalDescription').innerText = props.athlete_notes || "Rest / recovery day.";
    document.getElementById('modalSteps').innerText = "Rest day - no interval steps.";
    document.getElementById('modalMyWhooshLink').classList.add('hidden');
  }

  const modal = document.getElementById('workoutModal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  lucide.createIcons();
}

function viewTodayDetails() {
  if (currentEventData && currentEventData.id) {
    const calendarEvent = calendar.getEventById(currentEventData.id);
    if (calendarEvent) {
      openEventDetails(calendarEvent);
    }
  }
}

function closeWorkoutModal() {
  const modal = document.getElementById('workoutModal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

async function markEventStatus(newStatus) {
  if (!currentEventData || !currentEventData.id) return;
  try {
    const res = await fetch(`/api/calendar/${currentEventData.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus })
    });
    if (res.ok) {
      closeWorkoutModal();
      calendar.refetchEvents();
      loadStats();
      showToast(`Marked workout as ${newStatus}!`, 'success');
    }
  } catch (e) {
    showToast('Failed to update event', 'error');
  }
}

async function deleteCurrentEvent() {
  if (!currentEventData || !currentEventData.id) return;
  if (!confirm("Are you sure you want to remove this scheduled session?")) return;
  try {
    const res = await fetch(`/api/calendar/${currentEventData.id}`, { method: 'DELETE' });
    if (res.ok) {
      closeWorkoutModal();
      calendar.refetchEvents();
      loadStats();
      showToast('Workout removed from calendar.', 'info');
    }
  } catch (e) {
    showToast('Failed to delete workout', 'error');
  }
}

// ----------------- ADD EVENT / TARGET RACE -----------------

function openAddEventModal(dateStr = '', defaultType = 'race') {
  const modal = document.getElementById('addEventModal');
  const dateInput = document.getElementById('addEventDate');
  
  const today = new Date().toISOString().split('T')[0];
  dateInput.value = dateStr || today;

  switchAddEventType(defaultType);

  modal.classList.remove('hidden');
  modal.classList.add('flex');
  lucide.createIcons();
}

function closeAddEventModal() {
  const modal = document.getElementById('addEventModal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
  document.getElementById('addEventForm').reset();
}

function switchAddEventType(type) {
  document.getElementById('addEventType').value = type;

  const tabRace = document.getElementById('tabAddRace');
  const tabWorkout = document.getElementById('tabAddWorkout');
  const tabRest = document.getElementById('tabAddRest');
  const racePrio = document.getElementById('racePriorityGroup');
  const raceDetails = document.getElementById('raceDetailsGroup');
  const metrics = document.getElementById('metricsGroup');
  const titleLabel = document.getElementById('addEventNameLabel');
  const titleInput = document.getElementById('addEventTitle');
  const submitBtn = document.getElementById('btnSubmitAddEvent');

  const activeClass = "flex-1 py-1.5 px-3 rounded-md text-xs font-bold transition shadow-sm flex items-center justify-center gap-1.5";
  const inactiveClass = "flex-1 py-1.5 px-3 rounded-md text-xs font-medium text-slate-400 hover:text-white transition flex items-center justify-center gap-1.5";

  tabRace.className = type === 'race' ? `${activeClass} bg-amber-500 text-slate-950` : inactiveClass;
  tabWorkout.className = type === 'workout' ? `${activeClass} bg-sky-600 text-white` : inactiveClass;
  tabRest.className = type === 'rest' ? `${activeClass} bg-slate-700 text-white` : inactiveClass;

  if (type === 'race') {
    racePrio.classList.remove('hidden');
    raceDetails.classList.remove('hidden');
    metrics.classList.remove('hidden');
    titleLabel.innerText = "Race / Event Name";
    titleInput.placeholder = "e.g. Cape Town Cycle Tour";
    submitBtn.className = "px-5 py-2 bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold rounded-lg transition flex items-center gap-1.5 shadow-sm";
  } else if (type === 'workout') {
    racePrio.classList.add('hidden');
    raceDetails.classList.add('hidden');
    metrics.classList.remove('hidden');
    titleLabel.innerText = "Workout Title";
    titleInput.placeholder = "e.g. Outdoor Hill Repeats";
    submitBtn.className = "px-5 py-2 bg-sky-600 hover:bg-sky-500 text-white font-bold rounded-lg transition flex items-center gap-1.5 shadow-sm";
  } else if (type === 'rest') {
    racePrio.classList.add('hidden');
    raceDetails.classList.add('hidden');
    metrics.classList.add('hidden');
    titleLabel.innerText = "Rest Day Label";
    titleInput.value = "Rest & Active Recovery";
    submitBtn.className = "px-5 py-2 bg-slate-700 hover:bg-slate-600 text-white font-bold rounded-lg transition flex items-center gap-1.5 shadow-sm";
  }
}

async function submitAddEvent(e) {
  e.preventDefault();
  const type = document.getElementById('addEventType').value;
  const title = document.getElementById('addEventTitle').value.trim();
  const date = document.getElementById('addEventDate').value;
  const notes = document.getElementById('addEventNotes').value.trim();

  if (!title || !date) {
    showToast('Please provide a title and date.', 'error');
    return;
  }

  const payload = {
    date: date,
    title: title,
    event_type: type,
    athlete_notes: notes,
    coach_notes: type === 'race' ? `Target race event: ${title}` : ''
  };

  if (type === 'race') {
    payload.race_priority = document.getElementById('addRacePriority').value;
    payload.race_type = document.getElementById('addRaceType').value;
    const dist = parseFloat(document.getElementById('addRaceDistance').value);
    if (!isNaN(dist)) payload.target_distance_km = dist;
    payload.primary_zone = "Race";
    payload.planned_duration_minutes = parseFloat(document.getElementById('addEventDuration').value) || 180;
    payload.planned_tss = parseInt(document.getElementById('addEventTss').value, 10) || 180;
  } else if (type === 'workout') {
    payload.primary_zone = "Z3 - Tempo";
    payload.planned_duration_minutes = parseFloat(document.getElementById('addEventDuration').value) || 60;
    payload.planned_tss = parseInt(document.getElementById('addEventTss').value, 10) || 50;
  } else if (type === 'rest') {
    payload.primary_zone = "Rest Day";
    payload.planned_duration_minutes = 0;
    payload.planned_tss = 0;
  }

  try {
    const res = await fetch('/api/calendar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (res.ok) {
      closeAddEventModal();
      calendar.refetchEvents();
      loadStats();
      showToast(type === 'race' ? `Target Race "${title}" added to calendar!` : `Added "${title}" to calendar!`, 'success');
    } else {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || 'Failed to add event to calendar.', 'error');
    }
  } catch (err) {
    showToast(`Error adding event: ${err.message}`, 'error');
  }
}

// ----------------- AI PLAN GENERATOR -----------------

let lastGeneratedPlan = null;
let planTimerInterval = null;
let planTimerSec = 0;

function showPlanView(viewName) {
  const formView = document.getElementById('planFormView');
  const loadingView = document.getElementById('planLoadingView');
  const successView = document.getElementById('planSuccessView');
  const errorView = document.getElementById('planErrorView');
  const titleEl = document.getElementById('planModalTitle');

  if (formView) formView.classList.toggle('hidden', viewName !== 'form');
  if (loadingView) loadingView.classList.toggle('hidden', viewName !== 'loading');
  if (successView) successView.classList.toggle('hidden', viewName !== 'success');
  if (errorView) errorView.classList.toggle('hidden', viewName !== 'error');

  if (titleEl) {
    if (viewName === 'form') titleEl.innerText = "Generate Training Plan";
    else if (viewName === 'loading') titleEl.innerText = "AI Coach is Planning...";
    else if (viewName === 'success') titleEl.innerText = "Training Plan Scheduled!";
    else if (viewName === 'error') titleEl.innerText = "Plan Generation Error";
  }

  if (window.lucide) lucide.createIcons();
}

function startPlanLoadingAnimation() {
  planTimerSec = 0;
  const timerEl = document.getElementById('planTimerSeconds');
  if (timerEl) timerEl.innerText = "0s";

  const s1 = document.getElementById('loadStep1');
  const s2 = document.getElementById('loadStep2');
  const s3 = document.getElementById('loadStep3');
  const s4 = document.getElementById('loadStep4');

  if (s1) s1.className = "flex items-center gap-2 text-sky-400 font-semibold";
  if (s2) s2.className = "flex items-center gap-2 text-slate-500";
  if (s3) s3.className = "flex items-center gap-2 text-slate-500";
  if (s4) s4.className = "flex items-center gap-2 text-slate-500";

  clearInterval(planTimerInterval);
  planTimerInterval = setInterval(() => {
    planTimerSec++;
    if (timerEl) timerEl.innerText = `${planTimerSec}s`;

    if (planTimerSec === 4 && s2) {
      s1.className = "flex items-center gap-2 text-emerald-400";
      s2.className = "flex items-center gap-2 text-sky-400 font-semibold";
    } else if (planTimerSec === 8 && s3) {
      s2.className = "flex items-center gap-2 text-emerald-400";
      s3.className = "flex items-center gap-2 text-sky-400 font-semibold";
    } else if (planTimerSec === 14 && s4) {
      s3.className = "flex items-center gap-2 text-emerald-400";
      s4.className = "flex items-center gap-2 text-sky-400 font-semibold";
    }
  }, 1000);
}

function stopPlanLoadingAnimation() {
  clearInterval(planTimerInterval);
}

function resetPlanModalToForm() {
  stopPlanLoadingAnimation();
  showPlanView('form');
}

function openPlanModal() {
  const modal = document.getElementById('planModal');
  if (!document.getElementById('planStartDate').value) {
    const today = new Date().toISOString().split('T')[0];
    document.getElementById('planStartDate').value = today;
  }
  showPlanView('form');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
}

function closePlanModal() {
  stopPlanLoadingAnimation();
  const modal = document.getElementById('planModal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

function jumpToPlanOnCalendar() {
  if (lastGeneratedPlan && lastGeneratedPlan.start_date) {
    calendar.gotoDate(lastGeneratedPlan.start_date);
  }
  closePlanModal();
}

function reopenLastPlanSummary() {
  if (!lastGeneratedPlan) return;
  const modal = document.getElementById('planModal');
  showPlanView('success');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
}

function dismissPlanBanner() {
  const banner = document.getElementById('activePlanBanner');
  if (banner) {
    banner.classList.add('hidden');
    banner.classList.remove('flex');
  }
}

async function generatePlan() {
  const startDate = document.getElementById('planStartDate').value;
  const days = parseInt(document.getElementById('planDays').value, 10);
  const instructions = document.getElementById('planInstructions').value;
  const overwriteEl = document.getElementById('planOverwriteExisting');
  const overwrite = overwriteEl ? overwriteEl.checked : true;

  if (!startDate) {
    showToast('Please select a start date', 'error');
    return;
  }

  showPlanView('loading');
  startPlanLoadingAnimation();

  try {
    const res = await fetch('/api/coach/generate-plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_date: startDate,
        days: days,
        user_instructions: instructions,
        overwrite_existing: overwrite
      })
    });

    stopPlanLoadingAnimation();
    const data = await res.json();

    if (res.ok) {
      lastGeneratedPlan = data;
      calendar.refetchEvents();
      loadStats();

      // Populate Success View
      document.getElementById('planSuccessFocus').innerText = data.weekly_focus || 'Personalized Periodized Schedule';
      document.getElementById('planSuccessOverview').innerText = data.coach_overview || 'Your workouts have been scheduled to optimize fitness and recovery.';
      
      const modelBadge = document.getElementById('planSuccessModel');
      if (modelBadge) {
        if (data.fallback_used) {
          modelBadge.innerText = `Fallback: ${data.model_used}`;
          modelBadge.className = "px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20";
        } else {
          modelBadge.innerText = data.model_used || 'gemini-3.6-flash';
          modelBadge.className = "px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-500/10 text-sky-400 border border-sky-500/20";
        }
      }

      const metaEl = document.getElementById('planSuccessMeta');
      if (metaEl) {
        metaEl.innerHTML = `
          <span class="px-2 py-0.5 bg-slate-900 border border-slate-800 rounded font-semibold text-white">⚡ ${data.total_planned_tss || 0} Total TSS</span>
          <span class="px-2 py-0.5 bg-slate-900 border border-slate-800 rounded font-semibold text-white">⏱️ ${(data.total_planned_hours || 0).toFixed(1)} Hours</span>
          <span class="px-2 py-0.5 bg-slate-900 border border-slate-800 rounded font-semibold text-white">📅 ${data.days} Days (${data.start_date})</span>
        `;
      }

      const countEl = document.getElementById('planSuccessCount');
      if (countEl) {
        countEl.innerText = `${data.scheduled_count || (data.events ? data.events.length : 0)} items created`;
      }

      const listEl = document.getElementById('planSuccessList');
      if (listEl) {
        if (data.events && data.events.length > 0) {
          listEl.innerHTML = data.events.map(ev => {
            const isRace = ev.event_type === 'race' || (ev.title && ev.title.includes('[A-Race]'));
            const isRest = ev.status === 'rest' || (ev.title && ev.title.includes('Rest Day'));
            let badgeBg = "bg-sky-500/10 text-sky-400 border-sky-500/20";
            if (isRace) badgeBg = "bg-amber-500/10 text-amber-400 border-amber-500/20";
            else if (isRest) badgeBg = "bg-slate-800 text-slate-400 border-slate-700";

            return `
              <div class="flex items-center justify-between p-2 rounded-lg bg-slate-950/70 border border-slate-800/80 hover:border-slate-700 transition">
                <div class="flex items-center gap-2.5 overflow-hidden">
                  <span class="font-mono text-[11px] text-slate-400 font-semibold shrink-0">${ev.date}</span>
                  <span class="font-medium text-slate-200 truncate">${ev.title}</span>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                  <span class="px-1.5 py-0.5 rounded text-[10px] font-semibold border ${badgeBg}">${ev.primary_zone || 'Z2'}</span>
                  <span class="text-slate-400 text-[11px]">${ev.planned_duration_minutes}m</span>
                  <span class="text-sky-400 font-bold text-[11px] w-12 text-right">${ev.planned_tss} TSS</span>
                </div>
              </div>
            `;
          }).join('');
        } else {
          listEl.innerHTML = `<div class="text-slate-400 italic p-3 text-center">Workouts scheduled directly on calendar.</div>`;
        }
      }

      showPlanView('success');

      // Update active persistent plan banner on dashboard
      const banner = document.getElementById('activePlanBanner');
      if (banner) {
        document.getElementById('bannerPlanTitle').innerText = data.weekly_focus || 'AI Training Plan';
        document.getElementById('bannerPlanTss').innerText = `${data.total_planned_tss} TSS • ${data.scheduled_count} sessions`;
        document.getElementById('bannerPlanDates').innerText = `Active for ${data.start_date} (${data.days} days) • Generated via ${data.model_used}`;
        banner.classList.remove('hidden');
        banner.classList.add('flex');
      }

      showToast(`Training plan successfully created!`, 'success');
      appendCoachMessage(`I've scheduled your new ${days}-day plan: **${data.weekly_focus}** (${data.total_planned_tss} TSS).\n\n${data.coach_overview}`);
    } else {
      document.getElementById('planErrorMessage').innerText = data.detail || 'Plan generation failed. Please check your Gemini API key or model settings.';
      showPlanView('error');
    }
  } catch (e) {
    stopPlanLoadingAnimation();
    document.getElementById('planErrorMessage').innerText = `Connection error: ${e.message || 'Failed to reach WhooshCoach server'}`;
    showPlanView('error');
  }
}

// ----------------- AI COACH CHAT -----------------

async function handleCoachChat(e) {
  e.preventDefault();
  const input = document.getElementById('chatInput');
  const message = input.value.trim();
  if (!message) return;

  input.value = '';
  appendUserMessage(message);

  // Loading bubble
  const loadingBubble = document.createElement('div');
  loadingBubble.className = "bg-slate-800 border border-slate-700 rounded-xl p-3 text-slate-400 italic text-xs flex items-center gap-2";
  loadingBubble.innerHTML = `<span class="w-2 h-2 rounded-full bg-sky-400 animate-ping"></span> Coach is thinking...`;
  document.getElementById('chatMessages').appendChild(loadingBubble);
  scrollToChatBottom();

  try {
    const res = await fetch('/api/coach/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message })
    });
    const data = await res.json();
    loadingBubble.remove();
    appendCoachMessage(data.reply || "Sorry, I couldn't process that.");
  } catch (err) {
    loadingBubble.remove();
    appendCoachMessage("I had trouble reaching the AI service. Please verify your Gemini API key in Settings.");
  }
}

function sendQuickPrompt(promptText) {
  document.getElementById('chatInput').value = promptText;
  handleCoachChat(new Event('submit'));
}

function appendUserMessage(text) {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = "bg-sky-600 text-white rounded-xl p-3 ml-6 self-end shadow-sm";
  div.innerText = text;
  container.appendChild(div);
  scrollToChatBottom();
}

function appendCoachMessage(text) {
  const container = document.getElementById('chatMessages');
  const div = document.createElement('div');
  div.className = "bg-slate-800 border border-slate-700/80 rounded-xl p-3 text-slate-200 mr-6 shadow-sm leading-relaxed";
  div.innerText = text;
  container.appendChild(div);
  scrollToChatBottom();
}

function scrollToChatBottom() {
  const c = document.getElementById('chatMessages');
  c.scrollTop = c.scrollHeight;
}

function clearChatHistory() {
  document.getElementById('chatMessages').innerHTML = `
    <div class="bg-slate-800/80 border border-slate-700/60 rounded-xl p-3 text-slate-300">
      Chat history cleared. How can I help you with your training?
    </div>
  `;
}

// ----------------- MYWHOOSH WORKOUT LIBRARY -----------------

function switchSidebarTab(tab) {
  const btnCoach = document.getElementById('tabBtnCoach');
  const btnLib = document.getElementById('tabBtnLibrary');
  const tabCoach = document.getElementById('tabCoach');
  const tabLib = document.getElementById('tabLibrary');

  if (tab === 'coach') {
    btnCoach.className = "flex-1 py-1.5 px-3 rounded-md text-xs font-bold transition bg-sky-600 text-white shadow-sm flex items-center justify-center gap-1.5";
    btnLib.className = "flex-1 py-1.5 px-3 rounded-md text-xs font-medium text-slate-400 hover:text-white transition flex items-center justify-center gap-1.5";
    tabCoach.classList.remove('hidden');
    tabLib.classList.add('hidden');
  } else {
    btnLib.className = "flex-1 py-1.5 px-3 rounded-md text-xs font-bold transition bg-sky-600 text-white shadow-sm flex items-center justify-center gap-1.5";
    btnCoach.className = "flex-1 py-1.5 px-3 rounded-md text-xs font-medium text-slate-400 hover:text-white transition flex items-center justify-center gap-1.5";
    tabLib.classList.remove('hidden');
    tabCoach.classList.add('hidden');
    fetchLibraryWorkouts();
  }
  lucide.createIcons();
}

function debounceLibrarySearch() {
  clearTimeout(libraryDebounceTimer);
  libraryDebounceTimer = setTimeout(fetchLibraryWorkouts, 300);
}

async function fetchLibraryWorkouts() {
  const keyword = document.getElementById('libSearchInput').value;
  const zone = document.getElementById('libZoneFilter').value;
  const durVal = document.getElementById('libDurationFilter').value;
  
  let url = `/api/workouts?limit=40`;
  if (keyword) url += `&keyword=${encodeURIComponent(keyword)}`;
  if (zone) url += `&zone=${encodeURIComponent(zone)}`;
  if (durVal === '35') url += `&max_dur=35`;
  else if (durVal === '60') url += `&min_dur=36&max_dur=60`;
  else if (durVal === '90') url += `&min_dur=61&max_dur=90`;
  else if (durVal === '91') url += `&min_dur=91`;

  try {
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById('libResultCount').innerText = `${data.total} found`;

    const container = document.getElementById('libraryList');
    container.innerHTML = '';

    if (data.workouts.length === 0) {
      container.innerHTML = '<div class="text-slate-500 text-center py-6">No workouts matched your filter.</div>';
      return;
    }

    data.workouts.forEach(w => {
      const card = document.createElement('div');
      card.className = "bg-slate-950 border border-slate-800 hover:border-slate-700 rounded-lg p-2.5 transition flex flex-col gap-1 cursor-pointer";
      card.onclick = () => scheduleWorkoutOnCalendar(w);

      card.innerHTML = `
        <div class="flex items-center justify-between gap-1">
          <span class="font-bold text-slate-200 truncate">${w.title}</span>
          <span class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-slate-800 text-sky-400 shrink-0">${w.primary_zone.split(' - ')[0] || 'Z'}</span>
        </div>
        <div class="flex justify-between text-[11px] text-slate-400">
          <span>${w.duration}</span>
          <span>${w.tss} TSS</span>
          <span>IF ${w.intensity_factor}</span>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (e) {
    console.error(e);
  }
}

async function scheduleWorkoutOnCalendar(workout) {
  const targetDate = prompt(`Enter date to schedule "${workout.title}" (YYYY-MM-DD):`, new Date().toISOString().split('T')[0]);
  if (!targetDate) return;

  try {
    const res = await fetch('/api/calendar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        date: targetDate,
        title: workout.title,
        workout_id: workout.id,
        primary_zone: workout.primary_zone,
        planned_duration_minutes: workout.duration_minutes,
        planned_tss: workout.tss,
        coach_notes: `Manually added from MyWhoosh library.`
      })
    });
    if (res.ok) {
      calendar.refetchEvents();
      loadStats();
      showToast(`Scheduled "${workout.title}" on ${targetDate}!`, 'success');
    }
  } catch (e) {
    showToast('Failed to schedule workout', 'error');
  }
}

// ----------------- SETTINGS MODAL & OAUTH -----------------

function openSettingsModal() {
  const modal = document.getElementById('settingsModal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
}

function closeSettingsModal() {
  const modal = document.getElementById('settingsModal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

function toggleCoachingModeUI() {
  const isAuto = document.getElementById('modeAutonomous') && document.getElementById('modeAutonomous').checked;
  const targetContainer = document.getElementById('targetInputsContainer');
  const modeBadge = document.getElementById('coachingModeBadge');
  const cardAuto = document.getElementById('modeCardAutonomous');
  const cardTarget = document.getElementById('modeCardTargetDriven');

  if (isAuto) {
    if (targetContainer) targetContainer.classList.add('hidden');
    if (modeBadge) {
      modeBadge.innerText = "Autonomous Active";
      modeBadge.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-indigo-500/10 text-indigo-400 border border-indigo-500/20";
    }
    if (cardAuto) {
      cardAuto.className = "flex items-start gap-2.5 p-3 rounded-lg border border-indigo-500/40 bg-indigo-950/20 cursor-pointer hover:border-indigo-400 transition";
    }
    if (cardTarget) {
      cardTarget.className = "flex items-start gap-2.5 p-3 rounded-lg border border-slate-800 bg-slate-900/60 cursor-pointer hover:border-slate-700 transition";
    }
  } else {
    if (targetContainer) targetContainer.classList.remove('hidden');
    if (modeBadge) {
      modeBadge.innerText = "Custom Targets Active";
      modeBadge.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20";
    }
    if (cardAuto) {
      cardAuto.className = "flex items-start gap-2.5 p-3 rounded-lg border border-slate-800 bg-slate-900/60 cursor-pointer hover:border-slate-700 transition";
    }
    if (cardTarget) {
      cardTarget.className = "flex items-start gap-2.5 p-3 rounded-lg border border-amber-500/40 bg-amber-950/20 cursor-pointer hover:border-amber-400 transition";
    }
  }
}

async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) return;
    const s = await res.json();

    document.getElementById('settingFtp').value = s.ftp || 220;
    document.getElementById('settingMaxHr').value = s.max_hr || 185;
    document.getElementById('settingWeight').value = s.weight_kg || 75;
    document.getElementById('settingTargetHours').value = s.target_weekly_hours != null ? s.target_weekly_hours : '';
    document.getElementById('settingTargetTss').value = s.target_weekly_tss != null ? s.target_weekly_tss : '';
    document.getElementById('settingAvailableDays').value = s.available_days || '';
    document.getElementById('settingPrimaryGoal').value = s.primary_goal || '';
    
    // Coaching Mode
    const coachingMode = s.coaching_mode || 'autonomous';
    const modeAutoRadio = document.getElementById('modeAutonomous');
    const modeTargetRadio = document.getElementById('modeTargetDriven');
    if (modeAutoRadio && modeTargetRadio) {
      if (coachingMode === 'target_driven') {
        modeTargetRadio.checked = true;
      } else {
        modeAutoRadio.checked = true;
      }
      toggleCoachingModeUI();
    }

    // Gemini model
    if (document.getElementById('settingGeminiModel') && s.gemini_model) {
      document.getElementById('settingGeminiModel').value = s.gemini_model;
    }

    // MyWhoosh settings
    if (document.getElementById('settingMyWhooshEmail')) {
      document.getElementById('settingMyWhooshEmail').value = s.mywhoosh_email || '';
    }
    if (s.has_mywhoosh_password && document.getElementById('settingMyWhooshPassword')) {
      document.getElementById('settingMyWhooshPassword').placeholder = '•••••••• (Saved)';
    }

    // MyWhoosh status badge in header & modal
    const mwBadge = document.getElementById('mywhooshStatusBadge');
    const mwDot = document.getElementById('mywhooshStatusDot');
    const mwTxt = document.getElementById('mywhooshStatusText');
    const mwModalBadge = document.getElementById('settingsMyWhooshBadge');

    if (s.is_mywhoosh_connected) {
      if (mwBadge) mwBadge.classList.remove('hidden');
      if (mwDot) mwDot.className = "w-2 h-2 rounded-full bg-emerald-400";
      if (mwTxt) mwTxt.innerText = "MyWhoosh: Connected";
      if (mwModalBadge) {
        mwModalBadge.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20";
        mwModalBadge.innerText = "Connected";
      }
    } else {
      if (mwDot) mwDot.className = "w-2 h-2 rounded-full bg-amber-400";
      if (mwTxt) mwTxt.innerText = "MyWhoosh: Setup Needed";
      if (mwModalBadge) {
        mwModalBadge.className = "px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20";
        mwModalBadge.innerText = "Not Linked";
      }
    }

    if (s.last_mywhoosh_sync && document.getElementById('lastMyWhooshSyncLabel')) {
      document.getElementById('lastMyWhooshSyncLabel').innerText = `Last sync: ${s.last_mywhoosh_sync}`;
    }
  } catch (e) {
    console.error(e);
  }
}

async function saveSettings(silent = false) {
  const isAuto = document.getElementById('modeAutonomous') ? document.getElementById('modeAutonomous').checked : true;
  const coachingMode = isAuto ? 'autonomous' : 'target_driven';
  const hoursVal = document.getElementById('settingTargetHours').value.trim();
  const tssVal = document.getElementById('settingTargetTss').value.trim();

  const payload = {
    ftp: parseInt(document.getElementById('settingFtp').value, 10),
    max_hr: parseInt(document.getElementById('settingMaxHr').value, 10),
    weight_kg: parseFloat(document.getElementById('settingWeight').value),
    coaching_mode: coachingMode,
    target_weekly_hours: isAuto ? null : (hoursVal ? parseFloat(hoursVal) : null),
    target_weekly_tss: isAuto ? null : (tssVal ? parseInt(tssVal, 10) : null),
    available_days: document.getElementById('settingAvailableDays').value,
    primary_goal: document.getElementById('settingPrimaryGoal').value
  };

  const geminiKey = document.getElementById('settingGeminiKey').value.trim();
  if (geminiKey) payload.gemini_api_key = geminiKey;

  if (document.getElementById('settingGeminiModel')) {
    payload.gemini_model = document.getElementById('settingGeminiModel').value;
  }

  if (document.getElementById('settingMyWhooshEmail')) {
    payload.mywhoosh_email = document.getElementById('settingMyWhooshEmail').value.trim();
  }
  if (document.getElementById('settingMyWhooshPassword')) {
    const mwPass = document.getElementById('settingMyWhooshPassword').value.trim();
    if (mwPass) payload.mywhoosh_password = mwPass;
  }

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (res.ok) {
      if (!silent) {
        showToast('Settings saved successfully!', 'success');
        closeSettingsModal();
      }
      loadSettings();
      return true;
    } else {
      if (!silent) showToast('Failed to save settings.', 'error');
      return false;
    }
  } catch (e) {
    if (!silent) showToast('Connection error while saving settings', 'error');
    return false;
  }
}

async function testGeminiConnection() {
  await saveSettings(true);
  showToast('Testing Gemini API key & model...', 'info');
  const statusEl = document.getElementById('geminiTestStatus');
  if (statusEl) {
    statusEl.innerText = "Testing connection to Google Gemini...";
    statusEl.className = "text-[11px] text-sky-400 font-medium";
  }

  try {
    const res = await fetch('/api/coach/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        gemini_api_key: document.getElementById('settingGeminiKey').value.trim(),
        gemini_model: document.getElementById('settingGeminiModel').value
      })
    });
    let data = null;
    try { data = await res.json(); } catch (_) {}

    if (res.ok && data && data.success) {
      showToast(`Connected to ${data.model} in ${data.latency_ms}ms!`, 'success');
      if (statusEl) {
        const fbText = data.fallback_used ? ` (via fallback ${data.model})` : '';
        statusEl.innerText = `✓ Connected to ${data.model}${fbText} (${data.latency_ms}ms). Auto-fallback ready.`;
        statusEl.className = "text-[11px] text-emerald-400 font-medium";
      }
    } else {
      const errMsg = (data && (data.detail || data.message || data.error)) || `Status code ${res.status}`;
      showToast(`Gemini Test: ${errMsg}`, 'error');
      if (statusEl) {
        statusEl.innerText = `✗ Connection failed: ${errMsg}`;
        statusEl.className = "text-[11px] text-rose-400 font-medium";
      }
    }
  } catch (e) {
    showToast(`Network error testing Gemini: ${e.message}`, 'error');
    if (statusEl) {
      statusEl.innerText = `✗ Network error: ${e.message}`;
      statusEl.className = "text-[11px] text-rose-400 font-medium";
    }
  }
}

async function testMyWhooshConnection() {
  await saveSettings(true);
  showToast('Connecting to MyWhoosh cloud...', 'info');

  try {
    const res = await fetch('/api/mywhoosh/test', { method: 'POST' });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {}

    if (res.ok && data && data.success) {
      showToast('MyWhoosh connected successfully!', 'success');
      loadSettings();
    } else {
      const errMsg = (data && (data.detail || data.message || data.error)) || `Server returned status ${res.status}`;
      showToast(`MyWhoosh login: ${errMsg}`, 'error');
    }
  } catch (e) {
    console.error("Test connection error:", e);
    showToast(`Network error testing connection: ${e.message}`, 'error');
  }
}

async function triggerMyWhooshSync() {
  const btn = document.getElementById('btnSyncMyWhoosh');
  const iconWrap = document.getElementById('syncMyWhooshIconWrap');
  const txt = document.getElementById('syncMyWhooshText');

  if (btn) btn.disabled = true;
  if (iconWrap) iconWrap.classList.add('animate-spin');
  if (txt) txt.innerText = 'Syncing...';

  showToast('Syncing activities from MyWhoosh...', 'info');

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000); // 30s safety timeout

  try {
    const res = await fetch('/api/mywhoosh/sync', { 
      method: 'POST',
      signal: controller.signal
    });
    clearTimeout(timeoutId);

    let data = null;
    try {
      data = await res.json();
    } catch (_) {}

    if (res.ok && data && data.status === 'success') {
      calendar.refetchEvents();
      loadStats();

      const newRides = data.new_activities || 0;
      const cleanedWorkouts = data.cleaned_past_workouts || 0;

      let msg = '';
      if (newRides > 0 && cleanedWorkouts > 0) {
        msg = `MyWhoosh synced: ${newRides} new ride(s) logged & ${cleanedWorkouts} expired past workout(s) removed!`;
      } else if (newRides > 0) {
        msg = `MyWhoosh synced: ${newRides} new ride(s) logged!`;
      } else if (cleanedWorkouts > 0) {
        msg = `MyWhoosh synced: All caught up! Removed ${cleanedWorkouts} expired past workout(s).`;
      } else {
        msg = `MyWhoosh synced: Activities already up to date.`;
      }

      showToast(msg, 'success');

      if (data.adaptation) {
        appendCoachMessage(`**MyWhoosh Ride Logged!**\n\n${data.adaptation}`);
      }
      loadSettings();
    } else {
      const errMsg = (data && (data.message || data.detail || data.error)) || `Server returned status ${res.status}`;
      showToast(`MyWhoosh sync: ${errMsg}`, 'error');
    }
  } catch (e) {
    clearTimeout(timeoutId);
    console.error("MyWhoosh sync error:", e);
    if (e.name === 'AbortError') {
      showToast('Sync request timed out after 30s. The server may still be processing in the background.', 'error');
    } else {
      showToast(`Connection error during sync: ${e.message}`, 'error');
    }
  } finally {
    if (btn) btn.disabled = false;
    if (iconWrap) iconWrap.classList.remove('animate-spin');
    if (txt) txt.innerText = 'Sync MyWhoosh';
    // Clear any residual spinning classes on children
    document.querySelectorAll('#btnSyncMyWhoosh .animate-spin').forEach(el => el.classList.remove('animate-spin'));
  }
}

function togglePasswordVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ----------------- TOAST NOTIFICATIONS -----------------

function showToast(message, type = 'info') {
  const toast = document.getElementById('toast');
  const toastMsg = document.getElementById('toastMessage');
  const toastIcon = document.getElementById('toastIcon');

  toastMsg.innerText = message;
  if (type === 'success') {
    toastIcon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4 text-emerald-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`;
  } else if (type === 'error') {
    toastIcon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4 text-red-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/></svg>`;
  } else {
    toastIcon.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" class="w-4 h-4 text-sky-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="16" y2="12"/><line x1="12" x2="12.01" y1="8" y2="8"/></svg>`;
  }

  toast.classList.remove('translate-y-20', 'opacity-0', 'pointer-events-none');
  setTimeout(() => {
    toast.classList.add('translate-y-20', 'opacity-0', 'pointer-events-none');
  }, 4000);
}

// ----------------- SYSTEM LOGS VIEWER -----------------

function openLogsModal() {
  const modal = document.getElementById('logsModal');
  modal.classList.remove('hidden');
  modal.classList.add('flex');
  lucide.createIcons();
  fetchSystemLogs();
}

function closeLogsModal() {
  const modal = document.getElementById('logsModal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

async function fetchSystemLogs() {
  const container = document.getElementById('systemLogsContent');
  container.innerHTML = '<span class="text-slate-500 italic">Fetching real-time logs from server...</span>';

  try {
    const res = await fetch('/api/system/logs?limit=120');
    if (!res.ok) {
      container.innerText = `Error: Server returned HTTP ${res.status}`;
      return;
    }
    const logs = await res.json();
    if (!logs || logs.length === 0) {
      container.innerHTML = '<span class="text-slate-500 italic">No log entries recorded yet.</span>';
      return;
    }
    container.innerHTML = logs.map(l => {
      let colorClass = "text-slate-300";
      if (l.level === "ERROR" || l.level === "CRITICAL") colorClass = "text-rose-400 font-bold";
      else if (l.level === "WARNING") colorClass = "text-amber-300 font-medium";
      else if (l.level === "INFO") colorClass = "text-sky-300";
      return `<div class="py-0.5"><span class="text-slate-500 select-none">[${l.time}]</span> <span class="${colorClass}">[${l.level}]</span> <span class="text-slate-400 select-none">${l.logger}:</span> <span class="text-slate-200">${escapeHtml(l.message)}</span></div>`;
    }).join('');
    container.scrollTop = container.scrollHeight;
  } catch (e) {
    container.innerText = `Failed to fetch logs: ${e.message}`;
  }
}

function escapeHtml(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

