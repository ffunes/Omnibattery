/*
 * Marstek Venus Energy Manager — custom sidebar panel.
 *
 * Faithful port of the "MVEM" high-fidelity design handoff (Resumen view).
 * Vanilla custom element, no build step, no external deps. Home Assistant
 * injects `hass`, `panel`, `narrow` and `route`. We read entities from the
 * frontend registry (hass.entities) filtered by this integration's platform,
 * match them by translation_key (language/rename independent), aggregate them
 * into a single state model and render the themed dashboard:
 *
 *   - animated energy-flow diagram (Solar · Red · Casa · Batería + núcleo)
 *   - SOC ring hero ("Estado del sistema")
 *   - Potencia / Balance neto / Energía hoy / SOC mini-histórico / Diagnóstico
 *
 * The flow Grid/Home nodes use the entities the integration was configured
 * with, forwarded through the panel `config` payload (grid_entity / home_entity).
 * Solar uses the configured production sensor (solar_entity, external inverter)
 * when present, else falls back to per-battery PV sensors when the model exposes
 * an independent source.
 *
 * Design tokens (OKLCH) are embedded so the look matches the handoff exactly;
 * dark/light follows the user's HA theme (hass.themes.darkMode).
 *
 * Tabs: Resumen (this overview), Baterías (per-device cards + controls) and
 * Control (system-level entities grouped by feature — each capability's on/off
 * switch plus its CONFIG params: PD tuning, limits, thresholds). The DOM is
 * built once and patched in place on every hass update so the SVG particle
 * animation and ring transitions never restart.
 */

const FALLBACK_DOMAIN = "omnibattery";
const FALLBACK_TITLE = "Omnibattery";
const DAILY_OPERATION_BASE_INTERVALS = 96;
const DAILY_OPERATION_EXTENSION_INTERVALS = 48;
const DAILY_OPERATION_TOTAL_INTERVALS = DAILY_OPERATION_BASE_INTERVALS + DAILY_OPERATION_EXTENSION_INTERVALS;
const DAILY_OPERATION_TOTAL_HOURS = DAILY_OPERATION_TOTAL_INTERVALS / 4;
const DAILY_OPERATION_CONTEXT_HOURLY_BALANCE = 32;

// --- i18n ------------------------------------------------------------------
// All user-facing panel strings, keyed by a stable id and resolved at render
// time from the HA UI language (hass.locale.language). English is the base/
// fallback; the integration ships de/en/es/fr/nl, mirrored here. `{var}`
// placeholders are filled by _t(key, vars). Terminology matches the entity
// names in translations/*.json so the panel reads consistently with HA.
const I18N = {
  en: {
    subtitle: "Control Panel",
    live: "Live",
    tabResumen: "Overview", tabBaterias: "Batteries", tabControl: "Control",
    moreInfo: "Show history",
    zoomReset: "All",
    infoManufacturer: "Manufacturer", infoModel: "Model", infoSunSpecModel: "SunSpec model", infoSoftware: "Software", infoSerial: "Serial", infoInverter: "Inverter", infoPowerModule: "Power module",
    placeholderMsg: "This view is coming in a future phase. For now, use the Overview view.",
    cardFlow: "Energy flow", cardSoc: "System status", cardDaily: "Energy today",
    cardWeekly: "Weekly energy", cardPower: "Power", cardSocToday: "SOC · today",
    dailyOperationTitle: "Daily operation", dailyOperationDescription: "Observed and projected energy · 15 min",
    dailyOperationReal: "Real", dailyOperationCurrent: "Current", dailyOperationForecast: "Forecast",
    dailyOperationStale: "Data may be outdated", dailyOperationNoData: "No daily operation data",
    dailyOperationSolarWindow: "Solar window", dailyOperationSolarCharge: "Solar charge", dailyOperationGridCharge: "Grid charge", dailyOperationDischarge: "Battery discharge", dailyOperationEnergyToBattery: "Energy to battery", dailyOperationEnergyFromBattery: "Energy from battery", dailyOperationCause: "Cause", dailyOperationHourlyBalance: "Hourly net balance",
    dailyOperationNotNeeded: "Grid charge not needed", dailyOperationNoAction: "No action", dailyOperationSetpoint: "Until setpoint",
    dailyOperationDelay: "Charge delayed", dailyOperationUnlock: "Estimated unlock: {time}", dailyOperationAxis: "kWh/15 min",
    dailyOperationSolar: "Solar", dailyOperationConsumption: "Consumption", dailyOperationSoc: "Total SOC", dailyOperationLearned: "Learned profile",
    dailyOperationSinusoidal: "Sinusoidal fallback", dailyOperationGenericFallback: "Fallback estimate", dailyOperationZeroFallback: "Zero-solar fallback", dailyOperationFallback: "Fallback: {reason}", dailyOperationPlan: "Plan",
    dailyOperationObserved: "Observed", dailyOperationProjected: "Projected", dailyOperationPrevious: "Previous",
    dailyOperationNext: "Next", dailyOperationNow: "Now", dailyOperationCoverage: "Coverage", dailyOperationMode: "Mode",
    dailyOperationSource: "Source", dailyOperationDSTSkipped: "DST interval skipped", dailyOperationDSTRepeated: "Repeated local hour",
    dailyOperationProvider: "Provider forecast", dailyOperationConsumptionProfile: "Consumption profile", dailyOperationLegacyDaily: "Daily estimate", dailyOperationDynamicSchedule: "Dynamic schedule", dailyOperationTimeSlot: "Time slot", dailyOperationProfileProjection: "Profile projection", dailyOperationUnavailable: "Unavailable",
    dailyOperationRealtimeNoFuture: "Real-time price has no future calendar", dailyOperationUnknown: "Unknown",
    forecastToday: "Solar forecast · 00:05", solarRemaining: "Solar remaining", expectedConsumption: "Expected consumption",
    grid: "Grid", solar: "Solar", home: "Home", battery: "Battery",
    excludedDevices: "Excluded devices",
    importing: "Importing", exporting: "Exporting",
    charging: "Charging", discharging: "Discharging", idle: "Idle",
    selfConsumptionSuffix: "% self-consumption", units: "units",
    charge: "Charge", discharge: "Discharge", availOf: "of {value} available",
    charged: "Charged", discharged: "Discharged",
    gridImport: "Grid imported", gridExport: "Grid exported",
    now: "now", noData: "No data", imported: "Imported", exported: "Exported",
    diagTitle: "Integration status",
    diagIntegration: "Integration", diagPhaseProtection: "Phase protection", diagPdState: "PD state", diagNetBalance: "Net balance", diagAlarm: "Alarm",
    diagActiveBatteries: "Active batteries", diagNonResponsive: "No response",
    diagDischargeWindow: "Discharge window", diagPredictive: "Predictive charging", diagCurtailment: "Smart pre-discharge",
    diagPeak: "Peak shaving", diagWeeklyCharge: "Weekly charge", diagChargeDelay: "Charge delay",
    nResponsive: "{n} no response", none: "None",
    noBatteriesTitle: "No batteries",
    noBatteriesMsg: "No battery devices were detected in this integration.",
    healthCells: "Health & cells",
    mTemp: "Temperature", mVoltage: "Voltage", mCellMax: "Cell max", mCellMin: "Cell min",
    mCellDelta: "Δ cell", mCycles: "Cycles", mEfficiency: "Efficiency", mHysteresis: "Hysteresis",
    solarMppt: "Solar (MPPT)", controls: "Controls", deviceInfo: "Device information",
    acOutput: "AC output", acInput: "AC input", toHomeGrid: "To home / grid", fromAcBus: "From AC bus",
    inverterMode: "Inverter · {state}",
    offgrid: "Off-grid", infoComm: "Comm module",
    invBackup: "Backup", invUpdating: "Updating", invStandby: "Standby", invBypass: "Bypass",
    active: "Active", inactive: "Inactive",
    ctlEmpty: "No controls enabled. Enable them on the device (Settings → disabled entities).",
    ctlArrange: "Arrange", ctlArrangeHint: "Drag cards to reorder · controls are locked",
    ctlCols: "Columns", ctlRows: "Rows", ctlAuto: "Auto",
    ctlHide: "Hide card", ctlShow: "Show card", ctlHidden: "Hidden cards",
    sysEmptyTitle: "No controls available",
    sysEmptyMsg: "This integration exposes no system controls, or they are disabled. Enable them in Settings → entities.",
    bcAllowCharge: "Allow charge", bcAllowDischarge: "Allow discharge", bcBatteryManual: "Manual battery control", bcFroniusLock: "Keep Fronius/BYD internal control disabled",
    bcSocMax: "Max SOC", bcSocMin: "Min SOC", bcForceMode: "Force mode",
    bcChargePower: "Charge power", bcDischargePower: "Discharge power",
    bcMaxCharge: "Max charge", bcMaxDischarge: "Max discharge",
    bcChargeToSoc: "Charge to SOC", bcChargeHysteresis: "Charge hysteresis", bcBackup: "Backup function", bcOffgridMode: "Off-grid mode",
    bcBackupThreshold: "Backup threshold", bcVoltageTaper: "100% charge taper", bcBatteryPhase: "Battery phase",
    secPhaseProtection: "Three-phase protection", threePhaseProtection: "Three-phase current protection",
    secManual: "Manual mode", secOffgridMeter: "Off-grid meter mode", secVacation: "Vacation mode", itemEnable: "Enable",
    secTempLimit: "Temperature charge limit", itemTempLimitC: "Temperature limit", itemTempLimitBand: "Ramp band", itemTempLimitFloor: "Minimum charge power", itemTempApplyDischarge: "Also throttle discharge",
    itemMaxContracted: "Max contracted power", itemSolarSafety: "Solar safety margin", itemGridChargeMargin: "Grid charge margin", itemMinSocFloorEnable: "SOC floor", itemMinSocFloor: "Guaranteed minimum SOC",
    itemSocThreshold: "SOC threshold", itemPeakLimit: "Peak limit", itemExcludedPeakShaving: "Peak shaving for excluded devices",
    itemArbitrageMargin: "Min. arbitrage margin", itemRoundTripEfficiency: "Round-trip efficiency", itemMaxPrice: "Max price (charge)", itemDischargePrice: "Discharge price floor", itemPriceDischarge: "Discharge only above price", itemReevaluatePrices: "Re-evaluate prices now", itemNegativePriceCharging: "Charge at negative prices", itemSmartPredischarge: "Smart pre-discharge", itemNegativeThreshold: "Negative injection threshold", itemPredischargeReserve: "Pre-discharge reserve SOC", itemPredischargeExport: "Pre-discharge export cap",
    itemDelaySafety: "Safety margin", itemDelaySoc: "Delay target SOC", itemDelaySocEnable: "Delay target SOC enabled", itemDelayDeadband: "Balance deadband",
    secHourly: "Hourly balance", hourlyEsOnly: "Only useful in Spain (RD 244/2019) · detected country: {c}", secWeeklyFull: "Weekly full charge", itemWeeklyDay: "Full charge day", itemWeeklyDelay: "Wait for solar charge delay", itemHourlyTarget: "Target net balance", itemHourlyMaxOffset: "Max power offset", itemHourlyDeadband: "Deadband", itemHourlyHysteresis: "Hysteresis",
    secSlots: "Configured slots", itemSlot: "Slot",
    secExcluded: "Excluded devices", itemExcludedDevice: "Excluded device", itemSolarSurplus: "Solar surplus", itemDynamicPowerControl: "Dynamic power control", itemCoverHome: "Cover home", itemExclusionPct: "Exclusion %",
    secSysLimits: "System power limits", itemSysMaxCharge: "System max charge", itemSysMaxDischarge: "System max discharge",
    secCommon: "Common control",
    secPd: "PD controller (advanced)", itemPdEnable: "Use PD controller",
    secNoPd: "No-PD direct tracking", itemNoPdDelay: "Command delay",
    itemPdProfile: "Tuning profile", itemPdQuality: "Control quality",
    itemPdKp: "Proportional gain (Kp)", itemPdKd: "Derivative gain (Kd)", itemPdDeadband: "Deadband",
    itemPdMaxChange: "Max power change", itemPdDirHyst: "Direction hysteresis",
    itemPdMinCharge: "Min charge power", itemPdMinDischarge: "Min discharge power", itemPdRelayCooldown: "Relay cooldown", itemPdMinCycle: "Min cycle interval", itemPdTargetGrid: "Target grid power",
    slotSchedule: "Schedule", slotDays: "Days", slotAll: "All", slotMode: "Mode", slotManual: "Manual", slotPd: "PD",
    slotAllows: "Allows", slotChargeWord: "charge", slotDischargeWord: "discharge", slotNothing: "nothing",
    slotSocOverride: "SOC override", slotYes: "yes", slotPowerOverride: "Power override",
    slotStateLabel: "State", slotActiveWord: "active", slotInactiveWord: "inactive",
  },
  es: {
    subtitle: "Panel de Control",
    live: "En vivo",
    tabResumen: "Resumen", tabBaterias: "Baterías", tabControl: "Control",
    moreInfo: "Ver histórico",
    zoomReset: "Todo",
    infoManufacturer: "Fabricante", infoModel: "Modelo", infoSunSpecModel: "Modelo SunSpec", infoSoftware: "Software", infoSerial: "N.º serie", infoInverter: "Inversor", infoPowerModule: "Módulo de potencia",
    placeholderMsg: "Esta vista llegará en una próxima fase. Por ahora, usa la vista Resumen.",
    cardFlow: "Flujo de energía", cardSoc: "Estado del sistema", cardDaily: "Energía hoy",
    cardWeekly: "Energía semanal", cardPower: "Potencias", cardSocToday: "SOC · hoy",
    dailyOperationTitle: "Operación diaria", dailyOperationDescription: "Energía real y prevista · 15 min",
    dailyOperationReal: "Real", dailyOperationCurrent: "Actual", dailyOperationForecast: "Previsto",
    dailyOperationStale: "Datos posiblemente obsoletos", dailyOperationNoData: "Sin datos de operación diaria",
    dailyOperationSolarWindow: "Ventana solar", dailyOperationSolarCharge: "Carga solar", dailyOperationGridCharge: "Carga de red", dailyOperationDischarge: "Descarga de batería", dailyOperationEnergyToBattery: "Energía a batería", dailyOperationEnergyFromBattery: "Energía desde batería", dailyOperationCause: "Causa", dailyOperationHourlyBalance: "Balance neto horario",
    dailyOperationNotNeeded: "Carga de red no necesaria", dailyOperationNoAction: "Sin acción", dailyOperationSetpoint: "Hasta el setpoint",
    dailyOperationDelay: "Carga retrasada", dailyOperationUnlock: "Desbloqueo estimado: {time}", dailyOperationAxis: "kWh/15 min",
    dailyOperationSolar: "Solar", dailyOperationConsumption: "Consumo", dailyOperationSoc: "SOC total", dailyOperationLearned: "Perfil aprendido",
    dailyOperationSinusoidal: "Curva sinusoidal alternativa", dailyOperationGenericFallback: "Estimación alternativa", dailyOperationZeroFallback: "Previsión solar nula", dailyOperationFallback: "Estimación alternativa: {reason}", dailyOperationPlan: "Plan",
    dailyOperationObserved: "Observado", dailyOperationProjected: "Previsto", dailyOperationPrevious: "Anterior",
    dailyOperationNext: "Siguiente", dailyOperationNow: "Ahora", dailyOperationCoverage: "Cobertura", dailyOperationMode: "Modo",
    dailyOperationSource: "Fuente", dailyOperationDSTSkipped: "Intervalo DST omitido", dailyOperationDSTRepeated: "Hora local repetida",
    dailyOperationProvider: "Previsión del proveedor", dailyOperationConsumptionProfile: "Perfil de consumo", dailyOperationLegacyDaily: "Estimación diaria", dailyOperationDynamicSchedule: "Plan dinámico", dailyOperationTimeSlot: "Franja horaria", dailyOperationProfileProjection: "Proyección del perfil", dailyOperationUnavailable: "No disponible",
    dailyOperationRealtimeNoFuture: "El precio en tiempo real no tiene calendario futuro", dailyOperationUnknown: "Desconocido",
    forecastToday: "Previsión solar · 00:05", solarRemaining: "Solar restante", expectedConsumption: "Consumo esperado",
    grid: "Red", solar: "Solar", home: "Casa", battery: "Batería",
    excludedDevices: "Disp. excluidos",
    importing: "Importando", exporting: "Exportando",
    charging: "Cargando", discharging: "Descargando", idle: "Reposo",
    selfConsumptionSuffix: "% autoconsumo", units: "uds",
    charge: "Carga", discharge: "Descarga", availOf: "de {value} disponibles",
    charged: "Cargada", discharged: "Descargada",
    gridImport: "Red importada", gridExport: "Red exportada",
    now: "ahora", noData: "Sin datos", imported: "Importada", exported: "Exportada",
    diagTitle: "Estado de la integración",
    diagIntegration: "Integración", diagPhaseProtection: "Protección trifásica", diagPdState: "Estado PD", diagNetBalance: "Balance neto", diagAlarm: "Alarma",
    diagActiveBatteries: "Baterías activas", diagNonResponsive: "Sin respuesta",
    diagDischargeWindow: "Ventana de descarga", diagPredictive: "Carga predictiva", diagCurtailment: "Predescarga inteligente",
    diagPeak: "Reducción de picos", diagWeeklyCharge: "Carga semanal", diagChargeDelay: "Retardo de carga",
    nResponsive: "{n} sin respuesta", none: "Ninguna",
    noBatteriesTitle: "Sin baterías",
    noBatteriesMsg: "No se detectaron dispositivos de batería en esta integración.",
    healthCells: "Salud y celdas",
    mTemp: "Temperatura", mVoltage: "Voltaje", mCellMax: "Celda máx", mCellMin: "Celda mín",
    mCellDelta: "Δ celda", mCycles: "Ciclos", mEfficiency: "Eficiencia", mHysteresis: "Histéresis",
    solarMppt: "Solar (MPPT)", controls: "Controles", deviceInfo: "Información del dispositivo",
    acOutput: "Salida AC", acInput: "Entrada AC", toHomeGrid: "A casa / red", fromAcBus: "Desde bus AC",
    inverterMode: "Inversor · {state}",
    offgrid: "Offgrid", infoComm: "Módulo com.",
    invBackup: "Respaldo", invUpdating: "Actualizando", invStandby: "En espera", invBypass: "Bypass",
    active: "Activa", inactive: "Inactiva",
    ctlEmpty: "No hay controles habilitados. Actívalos en el dispositivo (Ajustes → entidades deshabilitadas).",
    ctlArrange: "Reordenar", ctlArrangeHint: "Arrastra las tarjetas para reordenar · controles bloqueados",
    ctlCols: "Columnas", ctlRows: "Filas", ctlAuto: "Auto",
    ctlHide: "Ocultar tarjeta", ctlShow: "Mostrar tarjeta", ctlHidden: "Tarjetas ocultas",
    sysEmptyTitle: "Sin controles disponibles",
    sysEmptyMsg: "Esta integración no expone controles de sistema, o están deshabilitados. Actívalos en Ajustes → entidades.",
    bcAllowCharge: "Permitir carga", bcAllowDischarge: "Permitir descarga", bcBatteryManual: "Control manual de batería", bcFroniusLock: "Mantener desactivado el control interno Fronius/BYD",
    bcSocMax: "SOC máximo", bcSocMin: "SOC mínimo", bcForceMode: "Modo forzado",
    bcChargePower: "Potencia de carga", bcDischargePower: "Potencia de descarga",
    bcMaxCharge: "Máx. carga", bcMaxDischarge: "Máx. descarga",
    bcChargeToSoc: "Cargar hasta SOC", bcChargeHysteresis: "Histéresis de carga", bcBackup: "Función de respaldo", bcOffgridMode: "Modo off-grid",
    bcBackupThreshold: "Umbral de respaldo", bcVoltageTaper: "Reducción carga 100%", bcBatteryPhase: "Fase de la batería",
    secPhaseProtection: "Protección trifásica", threePhaseProtection: "Protección de corriente trifásica",
    secManual: "Modo manual", secOffgridMeter: "Modo de medidor off-grid", secVacation: "Modo vacaciones", itemEnable: "Activar",
    secTempLimit: "Límite de carga por temperatura", itemTempLimitC: "Límite de temperatura", itemTempLimitBand: "Banda de reducción", itemTempLimitFloor: "Potencia de carga mínima", itemTempApplyDischarge: "Reducir también la descarga",
    itemMaxContracted: "Potencia contratada máx.", itemSolarSafety: "Margen de seguridad solar", itemGridChargeMargin: "Margen de carga de red", itemMinSocFloorEnable: "Suelo de SOC", itemMinSocFloor: "SOC mínimo garantizado",
    itemSocThreshold: "Umbral de SOC", itemPeakLimit: "Límite de pico", itemExcludedPeakShaving: "Reducción de picos para dispositivos excluidos",
    itemArbitrageMargin: "Margen mínimo de arbitraje", itemRoundTripEfficiency: "Eficiencia de ciclo completo", itemMaxPrice: "Precio máximo (carga)", itemDischargePrice: "Precio mínimo de descarga", itemPriceDischarge: "Descargar solo si precio alto", itemReevaluatePrices: "Reevaluar precios ahora", itemNegativePriceCharging: "Cargar con precios negativos", itemSmartPredischarge: "Predescarga inteligente", itemNegativeThreshold: "Umbral de inyección negativa", itemPredischargeReserve: "SOC de reserva de predescarga", itemPredischargeExport: "Límite de exportación de predescarga",
    itemDelaySafety: "Margen de seguridad", itemDelaySoc: "SOC objetivo de retardo", itemDelaySocEnable: "SOC objetivo de retardo activo", itemDelayDeadband: "Banda muerta de balance",
    secHourly: "Balance horario", hourlyEsOnly: "Solo útil en España (RD 244/2019) · país detectado: {c}", secWeeklyFull: "Carga semanal completa", itemWeeklyDay: "Día de carga completa", itemWeeklyDelay: "Esperar al retraso por solar", itemHourlyTarget: "Objetivo de balance neto", itemHourlyMaxOffset: "Offset máx. de potencia", itemHourlyDeadband: "Banda muerta", itemHourlyHysteresis: "Histéresis",
    secSlots: "Franjas configuradas", itemSlot: "Franja",
    secExcluded: "Dispositivos excluidos", itemExcludedDevice: "Dispositivo excluido", itemSolarSurplus: "Excedente solar", itemDynamicPowerControl: "Control dinámico de potencia", itemCoverHome: "Cubrir hogar", itemExclusionPct: "% excluido",
    secSysLimits: "Límites de potencia del sistema", itemSysMaxCharge: "Máx. carga del sistema", itemSysMaxDischarge: "Máx. descarga del sistema",
    secCommon: "Control común",
    secPd: "Controlador PD (avanzado)", itemPdEnable: "Usar controlador PD",
    secNoPd: "Seguimiento directo sin PD", itemNoPdDelay: "Retardo de orden",
    itemPdProfile: "Perfil de ajuste", itemPdQuality: "Calidad de control",
    itemPdKp: "Ganancia proporcional (Kp)", itemPdKd: "Ganancia derivativa (Kd)", itemPdDeadband: "Banda muerta",
    itemPdMaxChange: "Cambio máx. de potencia", itemPdDirHyst: "Histéresis de dirección",
    itemPdMinCharge: "Potencia mín. de carga", itemPdMinDischarge: "Potencia mín. de descarga", itemPdRelayCooldown: "Tiempo mín. de relé", itemPdMinCycle: "Intervalo mín. de ciclo", itemPdTargetGrid: "Potencia objetivo de red",
    slotSchedule: "Horario", slotDays: "Días", slotAll: "Todas", slotMode: "Modo", slotManual: "Manual", slotPd: "PD",
    slotAllows: "Permite", slotChargeWord: "carga", slotDischargeWord: "descarga", slotNothing: "nada",
    slotSocOverride: "SOC override", slotYes: "sí", slotPowerOverride: "Potencia override",
    slotStateLabel: "Estado", slotActiveWord: "activa", slotInactiveWord: "inactiva",
  },
  ca: {
    subtitle: "Tauler de control",
    live: "En directe",
    tabResumen: "Resum", tabBaterias: "Bateries", tabControl: "Control",
    moreInfo: "Veure històric",
    zoomReset: "Tot",
    infoManufacturer: "Fabricant", infoModel: "Model", infoSunSpecModel: "Model SunSpec", infoSoftware: "Programari", infoSerial: "Núm. sèrie", infoInverter: "Inversor", infoPowerModule: "Mòdul de potència",
    placeholderMsg: "Aquesta vista arribarà en una fase futura. De moment, fes servir la vista Resum.",
    cardFlow: "Flux d'energia", cardSoc: "Estat del sistema", cardDaily: "Energia avui",
    cardWeekly: "Energia setmanal", cardPower: "Potències", cardSocToday: "SOC · avui",
    dailyOperationTitle: "Operació diària", dailyOperationDescription: "Energia observada i prevista · 15 min",
    dailyOperationReal: "Real", dailyOperationCurrent: "Actual", dailyOperationForecast: "Previst",
    dailyOperationStale: "Dades possiblement obsoletes", dailyOperationNoData: "Sense dades d'operació diària",
    dailyOperationSolarWindow: "Finestra solar", dailyOperationSolarCharge: "Càrrega solar", dailyOperationGridCharge: "Càrrega de xarxa", dailyOperationDischarge: "Descàrrega de bateria", dailyOperationEnergyToBattery: "Energia a la bateria", dailyOperationEnergyFromBattery: "Energia des de la bateria", dailyOperationCause: "Causa", dailyOperationHourlyBalance: "Balanç net horari",
    dailyOperationNotNeeded: "Càrrega de xarxa no necessària", dailyOperationNoAction: "Sense acció", dailyOperationSetpoint: "Fins al setpoint",
    dailyOperationDelay: "Càrrega retardada", dailyOperationUnlock: "Desbloqueig estimat: {time}", dailyOperationAxis: "kWh/15 min",
    dailyOperationSolar: "Solar", dailyOperationConsumption: "Consum", dailyOperationSoc: "SOC total", dailyOperationLearned: "Perfil après",
    dailyOperationSinusoidal: "Corba sinusoidal alternativa", dailyOperationGenericFallback: "Estimació alternativa", dailyOperationZeroFallback: "Previsió solar nul·la", dailyOperationFallback: "Estimació alternativa: {reason}", dailyOperationPlan: "Pla",
    dailyOperationObserved: "Observat", dailyOperationProjected: "Previst", dailyOperationPrevious: "Anterior",
    dailyOperationNext: "Següent", dailyOperationNow: "Ara", dailyOperationCoverage: "Cobertura", dailyOperationMode: "Mode",
    dailyOperationSource: "Font", dailyOperationDSTSkipped: "Interval DST omès", dailyOperationDSTRepeated: "Hora local repetida",
    dailyOperationProvider: "Previsió del proveïdor", dailyOperationConsumptionProfile: "Perfil de consum", dailyOperationLegacyDaily: "Estimació diària", dailyOperationDynamicSchedule: "Pla dinàmic", dailyOperationTimeSlot: "Franja horària", dailyOperationProfileProjection: "Projecció del perfil", dailyOperationUnavailable: "No disponible",
    dailyOperationRealtimeNoFuture: "El preu en temps real no té calendari futur", dailyOperationUnknown: "Desconegut",
    forecastToday: "Previsió solar · 00:05", solarRemaining: "Solar restant", expectedConsumption: "Consum esperat",
    grid: "Xarxa", solar: "Solar", home: "Casa", battery: "Bateria",
    excludedDevices: "Disp. exclosos",
    importing: "Important", exporting: "Exportant",
    charging: "Carregant", discharging: "Descarregant", idle: "Repòs",
    selfConsumptionSuffix: "% autoconsum", units: "uts",
    charge: "Càrrega", discharge: "Descàrrega", availOf: "de {value} disponibles",
    charged: "Carregada", discharged: "Descarregada",
    gridImport: "Xarxa importada", gridExport: "Xarxa exportada",
    now: "ara", noData: "Sense dades", imported: "Importada", exported: "Exportada",
    diagTitle: "Estat de la integració",
    diagIntegration: "Integració", diagPhaseProtection: "Protecció trifàsica", diagPdState: "Estat PD", diagNetBalance: "Balanç net", diagAlarm: "Alarma",
    diagActiveBatteries: "Bateries actives", diagNonResponsive: "Sense resposta",
    diagDischargeWindow: "Finestra de descàrrega", diagPredictive: "Càrrega predictiva", diagCurtailment: "Predescàrrega intel·ligent",
    diagPeak: "Reducció de pics", diagWeeklyCharge: "Càrrega setmanal", diagChargeDelay: "Retard de càrrega",
    nResponsive: "{n} sense resposta", none: "Cap",
    noBatteriesTitle: "Sense bateries",
    noBatteriesMsg: "No s'han detectat dispositius de bateria en aquesta integració.",
    healthCells: "Salut i cel·les",
    mTemp: "Temperatura", mVoltage: "Voltatge", mCellMax: "Cel·la màx", mCellMin: "Cel·la mín",
    mCellDelta: "Δ cel·la", mCycles: "Cicles", mEfficiency: "Eficiència", mHysteresis: "Histèresi",
    solarMppt: "Solar (MPPT)", controls: "Controls", deviceInfo: "Informació del dispositiu",
    acOutput: "Sortida CA", acInput: "Entrada CA", toHomeGrid: "A casa / xarxa", fromAcBus: "Des del bus CA",
    inverterMode: "Inversor · {state}",
    offgrid: "Offgrid", infoComm: "Mòdul com.",
    invBackup: "Reserva", invUpdating: "Actualitzant", invStandby: "En espera", invBypass: "Bypass",
    active: "Activa", inactive: "Inactiva",
    ctlEmpty: "No hi ha controls habilitats. Activa'ls al dispositiu (Configuració → entitats deshabilitades).",
    sysEmptyTitle: "Sense controls disponibles",
    sysEmptyMsg: "Aquesta integració no exposa controls de sistema, o estan deshabilitats. Activa'ls a Configuració → entitats.",
    bcAllowCharge: "Permet la càrrega", bcAllowDischarge: "Permet la descàrrega", bcBatteryManual: "Control manual de la bateria", bcFroniusLock: "Mantén desactivat el control intern Fronius/BYD",
    bcSocMax: "SOC màxim", bcSocMin: "SOC mínim", bcForceMode: "Mode forçat",
    bcChargePower: "Potència de càrrega", bcDischargePower: "Potència de descàrrega",
    bcMaxCharge: "Màx. càrrega", bcMaxDischarge: "Màx. descàrrega",
    bcChargeToSoc: "Carregar fins a SOC", bcChargeHysteresis: "Histèresi de càrrega", bcBackup: "Funció de reserva", bcOffgridMode: "Mode fora de xarxa",
    bcBackupThreshold: "Llindar de reserva", bcVoltageTaper: "Reducció càrrega 100%", bcActiveBalance: "Balanç actiu",
    secManual: "Mode manual", secOffgridMeter: "Mode de mesurador off-grid", secVacation: "Mode vacances", itemEnable: "Activar",
    secTempLimit: "Límit de càrrega per temperatura", itemTempLimitC: "Límit de temperatura", itemTempLimitBand: "Banda de reducció", itemTempLimitFloor: "Potència de càrrega mínima", itemTempApplyDischarge: "Redueix també la descàrrega",
    itemMaxContracted: "Potència contractada màx.", itemSolarSafety: "Marge de seguretat solar", itemGridChargeMargin: "Marge de càrrega de xarxa", itemMinSocFloorEnable: "SOC Mínim", itemMinSocFloor: "SOC mínim garantit",
    itemSocThreshold: "Llindar de SOC", itemPeakLimit: "Límit de pic", itemExcludedPeakShaving: "Reducció de pics per a dispositius exclosos",
    itemArbitrageMargin: "Marge mínim d'arbitratge", itemRoundTripEfficiency: "Eficiència de cicle complet", itemMaxPrice: "Preu màxim (càrrega)", itemDischargePrice: "Preu mínim de descàrrega", itemPriceDischarge: "Descarregar només si preu alt", itemReevaluatePrices: "Reavaluar preus ara", itemNegativePriceCharging: "Carregar amb preus negatius", itemSmartPredischarge: "Predescàrrega intel·ligent", itemNegativeThreshold: "Llindar d'injecció negativa", itemPredischargeReserve: "SOC de reserva de predescàrrega", itemPredischargeExport: "Límit d'exportació de predescàrrega",
    itemDelaySafety: "Marge de seguretat", itemDelaySoc: "SOC objectiu de retard", itemDelaySocEnable: "SOC objectiu de retard actiu", itemDelayDeadband: "Banda morta de balanç",
    secHourly: "Balanç horari", hourlyEsOnly: "Només útil a Espanya (RD 244/2019) · país detectat: {c}", secWeeklyFull: "Càrrega setmanal completa", itemWeeklyDay: "Dia de càrrega completa", itemWeeklyDelay: "Espera el retard per solar", itemHourlyTarget: "Objectiu de balanç net", itemHourlyMaxOffset: "Offset màx. de potència", itemHourlyDeadband: "Banda morta", itemHourlyHysteresis: "Histèresi",
    secSlots: "Franges configurades", itemSlot: "Franja",
    secExcluded: "Dispositius exclosos", itemExcludedDevice: "Dispositiu exclòs", itemSolarSurplus: "Excedent solar", itemDynamicPowerControl: "Control dinàmic de potència", itemCoverHome: "Cobre la llar", itemExclusionPct: "% exclòs",
    secSysLimits: "Límits de potència del sistema", itemSysMaxCharge: "Màx. càrrega del sistema", itemSysMaxDischarge: "Màx. descàrrega del sistema",
    secCommon: "Control comú",
    secPd: "Controlador PD (avançat)",
    secNoPd: "Seguiment directe sense PD", itemNoPdDelay: "Retard d'ordre",
    itemPdProfile: "Perfil d'ajust", itemPdQuality: "Qualitat de control",
    itemPdKp: "Guany proporcional (Kp)", itemPdKd: "Guany derivatiu (Kd)", itemPdDeadband: "Banda morta",
    itemPdMaxChange: "Canvi màx. de potència", itemPdDirHyst: "Histèresi de direcció",
    itemPdMinCharge: "Potència mín. de càrrega", itemPdMinDischarge: "Potència mín. de descàrrega", itemPdRelayCooldown: "Temps mín. de relé", itemPdMinCycle: "Interval mín. de cicle", itemPdTargetGrid: "Potència objectiu de xarxa",
    slotSchedule: "Horari", slotDays: "Dies", slotAll: "Totes", slotMode: "Mode", slotManual: "Manual", slotPd: "PD",
    slotAllows: "Permet", slotChargeWord: "càrrega", slotDischargeWord: "descàrrega", slotNothing: "res",
    slotSocOverride: "SOC override", slotYes: "sí", slotPowerOverride: "Potència override",
    slotStateLabel: "Estat", slotActiveWord: "activa", slotInactiveWord: "inactiva",
  },
  de: {
    subtitle: "Bedienfeld",
    live: "Live",
    tabResumen: "Übersicht", tabBaterias: "Batterien", tabControl: "Steuerung",
    moreInfo: "Verlauf anzeigen",
    zoomReset: "Alles",
    infoManufacturer: "Hersteller", infoModel: "Modell", infoSunSpecModel: "SunSpec-Modell", infoSoftware: "Software", infoSerial: "Seriennr.", infoInverter: "Wechselrichter", infoPowerModule: "Leistungsmodul",
    placeholderMsg: "Diese Ansicht kommt in einer späteren Phase. Nutze vorerst die Übersicht.",
    cardFlow: "Energiefluss", cardSoc: "Systemstatus", cardDaily: "Energie heute",
    cardWeekly: "Wochenenergie", cardPower: "Leistung", cardSocToday: "SOC · heute",
    dailyOperationTitle: "Tagesbetrieb", dailyOperationDescription: "Beobachtete und prognostizierte Energie · 15 Min.",
    dailyOperationReal: "Ist", dailyOperationCurrent: "Aktuell", dailyOperationForecast: "Prognose",
    dailyOperationStale: "Daten möglicherweise veraltet", dailyOperationNoData: "Keine Tagesbetriebsdaten",
    dailyOperationSolarWindow: "Solarfenster", dailyOperationSolarCharge: "Solarladung", dailyOperationGridCharge: "Netzladung", dailyOperationDischarge: "Batterieentladung", dailyOperationEnergyToBattery: "Energie zur Batterie", dailyOperationEnergyFromBattery: "Energie aus der Batterie", dailyOperationCause: "Ursache", dailyOperationHourlyBalance: "Stündlicher Netzausgleich",
    dailyOperationNotNeeded: "Keine Netzladung erforderlich", dailyOperationNoAction: "Keine Aktion", dailyOperationSetpoint: "Bis zum Sollwert",
    dailyOperationDelay: "Laden verzögert", dailyOperationUnlock: "Voraussichtliche Freigabe: {time}", dailyOperationAxis: "kWh/15 Min.",
    dailyOperationSolar: "Solar", dailyOperationConsumption: "Verbrauch", dailyOperationSoc: "Gesamt-SOC", dailyOperationLearned: "Gelerntes Profil",
    dailyOperationSinusoidal: "Sinusförmige Ersatzkurve", dailyOperationGenericFallback: "Ersatzprognose", dailyOperationZeroFallback: "Solarprognose ohne Ertrag", dailyOperationFallback: "Ersatzprognose: {reason}", dailyOperationPlan: "Plan",
    dailyOperationObserved: "Beobachtet", dailyOperationProjected: "Prognostiziert", dailyOperationPrevious: "Zurück",
    dailyOperationNext: "Weiter", dailyOperationNow: "Jetzt", dailyOperationCoverage: "Abdeckung", dailyOperationMode: "Modus",
    dailyOperationSource: "Quelle", dailyOperationDSTSkipped: "DST-Intervall übersprungen", dailyOperationDSTRepeated: "Wiederholte Ortszeit",
    dailyOperationProvider: "Anbieterprognose", dailyOperationConsumptionProfile: "Verbrauchsprofil", dailyOperationLegacyDaily: "Tagesschätzung", dailyOperationDynamicSchedule: "Dynamischer Zeitplan", dailyOperationTimeSlot: "Zeitfenster", dailyOperationProfileProjection: "Profilprojektion", dailyOperationUnavailable: "Nicht verfügbar",
    dailyOperationRealtimeNoFuture: "Echtzeitpreis hat keinen Zukunftskalender", dailyOperationUnknown: "Unbekannt",
    forecastToday: "Solarprognose · 00:05", solarRemaining: "Verbleibende Solarenergie", expectedConsumption: "Erwarteter Verbrauch",
    grid: "Netz", solar: "Solar", home: "Haus", battery: "Batterie",
    excludedDevices: "Ausgeschl. Geräte",
    importing: "Bezug", exporting: "Einspeisung",
    charging: "Laden", discharging: "Entladen", idle: "Bereit",
    selfConsumptionSuffix: "% Eigenverbrauch", units: "Einh.",
    charge: "Laden", discharge: "Entladen", availOf: "von {value} verfügbar",
    charged: "Geladen", discharged: "Entladen",
    gridImport: "Netzbezug", gridExport: "Netzeinspeisung",
    now: "jetzt", noData: "Keine Daten", imported: "Bezug", exported: "Einspeisung",
    diagTitle: "Integrationsstatus",
    diagIntegration: "Integration", diagPhaseProtection: "Phasenschutz", diagPdState: "PD-Status", diagNetBalance: "Netto-Balance", diagAlarm: "Alarm",
    diagActiveBatteries: "Aktive Batterien", diagNonResponsive: "Keine Antwort",
    diagDischargeWindow: "Entladefenster", diagPredictive: "Prädiktives Laden", diagCurtailment: "Intelligente Vorentladung",
    diagPeak: "Spitzenlastkappung", diagWeeklyCharge: "Wöchentliche Ladung", diagChargeDelay: "Ladeverzögerung",
    nResponsive: "{n} ohne Antwort", none: "Keine",
    noBatteriesTitle: "Keine Batterien",
    noBatteriesMsg: "In dieser Integration wurden keine Batteriegeräte erkannt.",
    healthCells: "Zustand & Zellen",
    mTemp: "Temperatur", mVoltage: "Spannung", mCellMax: "Zelle max", mCellMin: "Zelle min",
    mCellDelta: "Δ Zelle", mCycles: "Zyklen", mEfficiency: "Effizienz", mHysteresis: "Hysterese",
    solarMppt: "Solar (MPPT)", controls: "Steuerung", deviceInfo: "Geräteinformationen",
    acOutput: "AC-Ausgang", acInput: "AC-Eingang", toHomeGrid: "Zu Haus / Netz", fromAcBus: "Vom AC-Bus",
    inverterMode: "Wechselrichter · {state}",
    offgrid: "Inselbetrieb", infoComm: "Komm.-Modul",
    invBackup: "Backup", invUpdating: "Aktualisierung", invStandby: "Standby", invBypass: "Bypass",
    active: "Aktiv", inactive: "Inaktiv",
    ctlEmpty: "Keine Steuerungen aktiviert. Aktiviere sie am Gerät (Einstellungen → deaktivierte Entitäten).",
    sysEmptyTitle: "Keine Steuerungen verfügbar",
    sysEmptyMsg: "Diese Integration stellt keine Systemsteuerungen bereit oder sie sind deaktiviert. Aktiviere sie in Einstellungen → Entitäten.",
    bcAllowCharge: "Laden erlauben", bcAllowDischarge: "Entladen erlauben", bcBatteryManual: "Manuelle Batteriesteuerung", bcFroniusLock: "Fronius/BYD-Eigensteuerung dauerhaft sperren",
    bcSocMax: "Max. SOC", bcSocMin: "Min. SOC", bcForceMode: "Betriebsmodus erzwingen",
    bcChargePower: "Ladeleistung", bcDischargePower: "Entladeleistung",
    bcMaxCharge: "Max. Ladeleistung", bcMaxDischarge: "Max. Entladeleistung",
    bcChargeToSoc: "Laden bis SOC", bcChargeHysteresis: "Ladehysterese", bcBackup: "Backup-Funktion", bcOffgridMode: "Inselnetz-Modus",
    bcBackupThreshold: "Backup-Schwelle", bcVoltageTaper: "100%-Ladungsreduktion", bcActiveBalance: "Aktiver Zellabgleich",
    secManual: "Manueller Modus", secOffgridMeter: "Off-Grid-Zählermodus", secVacation: "Urlaubsmodus", itemEnable: "Aktivieren",
    secTempLimit: "Temperaturbasierte Ladebegrenzung", itemTempLimitC: "Temperaturgrenze", itemTempLimitBand: "Drosselbereich", itemTempLimitFloor: "Minimale Ladeleistung", itemTempApplyDischarge: "Auch Entladung drosseln",
    itemMaxContracted: "Max. Vertragsleistung", itemSolarSafety: "Sicherheitspuffer Solar", itemGridChargeMargin: "Netzladungs-Marge", itemMinSocFloorEnable: "SOC-Untergrenze", itemMinSocFloor: "Garantierter Mindest-SOC",
    itemSocThreshold: "SOC-Schwelle", itemPeakLimit: "Spitzenlimit", itemExcludedPeakShaving: "Spitzenlastkappung für ausgeschlossene Geräte",
    itemArbitrageMargin: "Min. Arbitragemarge", itemRoundTripEfficiency: "Round-Trip-Wirkungsgrad", itemMaxPrice: "Max. Preis (Laden)", itemDischargePrice: "Entlade-Preisuntergrenze", itemPriceDischarge: "Nur über Preis entladen", itemReevaluatePrices: "Preise jetzt neu bewerten", itemNegativePriceCharging: "Bei negativen Preisen laden", itemSmartPredischarge: "Intelligente Vorentladung", itemNegativeThreshold: "Schwelle für negative Einspeisung", itemPredischargeReserve: "Vorentlade-Reserve-SOC", itemPredischargeExport: "Vorentlade-Exportlimit",
    itemDelaySafety: "Sicherheitspuffer", itemDelaySoc: "Verzögerungs-Ziel-SOC", itemDelaySocEnable: "Verzögerungs-Ziel-SOC aktiv", itemDelayDeadband: "Bilanz-Totband",
    secHourly: "Stündliche Balance", hourlyEsOnly: "Nur in Spanien sinnvoll (RD 244/2019) · erkanntes Land: {c}", secWeeklyFull: "Wöchentliche Vollladung", itemWeeklyDay: "Tag der Vollladung", itemWeeklyDelay: "Auf Solar-Ladeverzögerung warten", itemHourlyTarget: "Ziel-Nettobalance", itemHourlyMaxOffset: "Max. Leistungs-Offset", itemHourlyDeadband: "Totband", itemHourlyHysteresis: "Hysterese",
    secSlots: "Konfigurierte Zeitfenster", itemSlot: "Zeitfenster",
    secExcluded: "Ausgeschlossene Geräte", itemExcludedDevice: "Ausgeschlossenes Gerät", itemSolarSurplus: "Solarüberschuss", itemDynamicPowerControl: "Dynamische Leistungsregelung", itemCoverHome: "Haus decken", itemExclusionPct: "Ausschluss %",
    secSysLimits: "System-Leistungsgrenzen", itemSysMaxCharge: "System-Max.-Ladeleistung", itemSysMaxDischarge: "System-Max.-Entladeleistung",
    secCommon: "Gemeinsame Regelung",
    secPd: "PD-Regler (erweitert)",
    secNoPd: "Direkte Nachführung ohne PD", itemNoPdDelay: "Befehlsverzögerung",
    itemPdProfile: "Tuning-Profil", itemPdQuality: "Regelqualität",
    itemPdKp: "Proportionalverstärkung (Kp)", itemPdKd: "Differenzialverstärkung (Kd)", itemPdDeadband: "Totband",
    itemPdMaxChange: "Max. Leistungsänderung", itemPdDirHyst: "Richtungshysterese",
    itemPdMinCharge: "Min. Ladeleistung", itemPdMinDischarge: "Min. Entladeleistung", itemPdRelayCooldown: "Relais-Mindestlaufzeit", itemPdMinCycle: "Min. Zyklusintervall", itemPdTargetGrid: "Ziel-Netzleistung",
    slotSchedule: "Zeitplan", slotDays: "Tage", slotAll: "Alle", slotMode: "Modus", slotManual: "Manuell", slotPd: "PD",
    slotAllows: "Erlaubt", slotChargeWord: "Laden", slotDischargeWord: "Entladen", slotNothing: "nichts",
    slotSocOverride: "SOC-Override", slotYes: "ja", slotPowerOverride: "Leistungs-Override",
    slotStateLabel: "Status", slotActiveWord: "aktiv", slotInactiveWord: "inaktiv",
  },
  fr: {
    subtitle: "Panneau de contrôle",
    live: "En direct",
    tabResumen: "Résumé", tabBaterias: "Batteries", tabControl: "Contrôle",
    moreInfo: "Voir l'historique",
    zoomReset: "Tout",
    infoManufacturer: "Fabricant", infoModel: "Modèle", infoSunSpecModel: "Modèle SunSpec", infoSoftware: "Logiciel", infoSerial: "N° série", infoInverter: "Onduleur", infoPowerModule: "Module de puissance",
    placeholderMsg: "Cette vue arrivera dans une phase ultérieure. Pour l'instant, utilisez la vue Résumé.",
    cardFlow: "Flux d'énergie", cardSoc: "État du système", cardDaily: "Énergie aujourd'hui",
    cardWeekly: "Énergie hebdomadaire", cardPower: "Puissances", cardSocToday: "SOC · aujourd'hui",
    dailyOperationTitle: "Opération quotidienne", dailyOperationDescription: "Énergie observée et prévue · 15 min",
    dailyOperationReal: "Réel", dailyOperationCurrent: "Actuel", dailyOperationForecast: "Prévision",
    dailyOperationStale: "Données potentiellement obsolètes", dailyOperationNoData: "Aucune donnée d'opération quotidienne",
    dailyOperationSolarWindow: "Fenêtre solaire", dailyOperationSolarCharge: "Charge solaire", dailyOperationGridCharge: "Charge réseau", dailyOperationDischarge: "Décharge batterie", dailyOperationEnergyToBattery: "Énergie vers la batterie", dailyOperationEnergyFromBattery: "Énergie depuis la batterie", dailyOperationCause: "Cause", dailyOperationHourlyBalance: "Bilan net horaire",
    dailyOperationNotNeeded: "Charge réseau non nécessaire", dailyOperationNoAction: "Aucune action", dailyOperationSetpoint: "Jusqu'au point de consigne",
    dailyOperationDelay: "Charge retardée", dailyOperationUnlock: "Déblocage estimé : {time}", dailyOperationAxis: "kWh/15 min",
    dailyOperationSolar: "Solaire", dailyOperationConsumption: "Consommation", dailyOperationSoc: "SOC total", dailyOperationLearned: "Profil appris",
    dailyOperationSinusoidal: "Courbe sinusoïdale de secours", dailyOperationGenericFallback: "Estimation de secours", dailyOperationZeroFallback: "Prévision solaire nulle", dailyOperationFallback: "Estimation de secours : {reason}", dailyOperationPlan: "Plan",
    dailyOperationObserved: "Observé", dailyOperationProjected: "Prévu", dailyOperationPrevious: "Précédent",
    dailyOperationNext: "Suivant", dailyOperationNow: "Maintenant", dailyOperationCoverage: "Couverture", dailyOperationMode: "Mode",
    dailyOperationSource: "Source", dailyOperationDSTSkipped: "Intervalle DST ignoré", dailyOperationDSTRepeated: "Heure locale répétée",
    dailyOperationProvider: "Prévision du fournisseur", dailyOperationConsumptionProfile: "Profil de consommation", dailyOperationLegacyDaily: "Estimation quotidienne", dailyOperationDynamicSchedule: "Plan dynamique", dailyOperationTimeSlot: "Créneau horaire", dailyOperationProfileProjection: "Projection du profil", dailyOperationUnavailable: "Indisponible",
    dailyOperationRealtimeNoFuture: "Le prix en temps réel n'a pas de calendrier futur", dailyOperationUnknown: "Inconnu",
    forecastToday: "Prévision solaire · 00:05", solarRemaining: "Solaire restant", expectedConsumption: "Consommation prévue",
    grid: "Réseau", solar: "Solaire", home: "Maison", battery: "Batterie",
    excludedDevices: "Appareils exclus",
    importing: "Importation", exporting: "Exportation",
    charging: "Charge", discharging: "Décharge", idle: "Repos",
    selfConsumptionSuffix: "% autoconsommation", units: "unités",
    charge: "Charge", discharge: "Décharge", availOf: "sur {value} disponibles",
    charged: "Chargée", discharged: "Déchargée",
    gridImport: "Réseau importé", gridExport: "Réseau exporté",
    now: "maintenant", noData: "Aucune donnée", imported: "Importée", exported: "Exportée",
    diagTitle: "État de l'intégration",
    diagIntegration: "Intégration", diagPhaseProtection: "Protection de phase", diagPdState: "État PD", diagNetBalance: "Bilan net", diagAlarm: "Alarme",
    diagActiveBatteries: "Batteries actives", diagNonResponsive: "Sans réponse",
    diagDischargeWindow: "Fenêtre de décharge", diagPredictive: "Charge prédictive", diagCurtailment: "Pré-décharge intelligente",
    diagPeak: "Écrêtement de pointe", diagWeeklyCharge: "Charge hebdomadaire", diagChargeDelay: "Délai de charge",
    nResponsive: "{n} sans réponse", none: "Aucune",
    noBatteriesTitle: "Aucune batterie",
    noBatteriesMsg: "Aucun appareil de batterie n'a été détecté dans cette intégration.",
    healthCells: "Santé et cellules",
    mTemp: "Température", mVoltage: "Tension", mCellMax: "Cellule max", mCellMin: "Cellule min",
    mCellDelta: "Δ cellule", mCycles: "Cycles", mEfficiency: "Efficacité", mHysteresis: "Hystérésis",
    solarMppt: "Solaire (MPPT)", controls: "Contrôles", deviceInfo: "Informations sur l'appareil",
    acOutput: "Sortie CA", acInput: "Entrée CA", toHomeGrid: "Vers maison / réseau", fromAcBus: "Depuis bus CA",
    inverterMode: "Onduleur · {state}",
    offgrid: "Hors réseau", infoComm: "Module comm.",
    invBackup: "Secours", invUpdating: "Mise à jour", invStandby: "En attente", invBypass: "Bypass",
    active: "Active", inactive: "Inactive",
    ctlEmpty: "Aucun contrôle activé. Activez-les sur l'appareil (Paramètres → entités désactivées).",
    sysEmptyTitle: "Aucun contrôle disponible",
    sysEmptyMsg: "Cette intégration n'expose aucun contrôle système, ou ils sont désactivés. Activez-les dans Paramètres → entités.",
    bcAllowCharge: "Autoriser la charge", bcAllowDischarge: "Autoriser la décharge", bcBatteryManual: "Contrôle manuel de la batterie", bcFroniusLock: "Maintenir le contrôle interne Fronius/BYD désactivé",
    bcSocMax: "SOC max.", bcSocMin: "SOC min.", bcForceMode: "Mode forcé",
    bcChargePower: "Puissance de charge", bcDischargePower: "Puissance de décharge",
    bcMaxCharge: "Charge max.", bcMaxDischarge: "Décharge max.",
    bcChargeToSoc: "Charger jusqu'à SOC", bcChargeHysteresis: "Hystérésis de charge", bcBackup: "Fonction de secours", bcOffgridMode: "Mode hors-réseau",
    bcBackupThreshold: "Seuil de secours", bcVoltageTaper: "Réduction charge 100%", bcActiveBalance: "Équilibrage actif",
    secManual: "Mode manuel", secOffgridMeter: "Mode compteur hors réseau", secVacation: "Mode vacances", itemEnable: "Activer",
    secTempLimit: "Limite de charge par température", itemTempLimitC: "Limite de température", itemTempLimitBand: "Plage de réduction", itemTempLimitFloor: "Puissance de charge minimale", itemTempApplyDischarge: "Réduire aussi la décharge",
    itemMaxContracted: "Puissance contractuelle max.", itemSolarSafety: "Marge de sécurité solaire", itemGridChargeMargin: "Marge de charge réseau", itemMinSocFloorEnable: "Plancher SOC", itemMinSocFloor: "SOC minimum garanti",
    itemSocThreshold: "Seuil SOC", itemPeakLimit: "Limite de pointe", itemExcludedPeakShaving: "Écrêtement des pointes pour appareils exclus",
    itemArbitrageMargin: "Marge d'arbitrage min.", itemRoundTripEfficiency: "Rendement aller-retour", itemMaxPrice: "Prix max. (charge)", itemDischargePrice: "Prix plancher de décharge", itemPriceDischarge: "Décharger si prix élevé", itemReevaluatePrices: "Réévaluer les prix", itemNegativePriceCharging: "Charger aux prix négatifs", itemSmartPredischarge: "Pré-décharge intelligente", itemNegativeThreshold: "Seuil d'injection négative", itemPredischargeReserve: "SOC de réserve de pré-décharge", itemPredischargeExport: "Limite d'export de pré-décharge",
    itemDelaySafety: "Marge de sécurité", itemDelaySoc: "SOC cible du délai", itemDelaySocEnable: "SOC cible du délai actif", itemDelayDeadband: "Bande morte de bilan",
    secHourly: "Bilan horaire", hourlyEsOnly: "Utile uniquement en Espagne (RD 244/2019) · pays détecté : {c}", secWeeklyFull: "Charge complète hebdomadaire", itemWeeklyDay: "Jour de charge complète", itemWeeklyDelay: "Attendre le délai de charge solaire", itemHourlyTarget: "Cible bilan net", itemHourlyMaxOffset: "Décalage max. puissance", itemHourlyDeadband: "Bande morte", itemHourlyHysteresis: "Hystérésis",
    secSlots: "Créneaux configurés", itemSlot: "Créneau",
    secExcluded: "Appareils exclus", itemExcludedDevice: "Appareil exclu", itemSolarSurplus: "Surplus solaire", itemDynamicPowerControl: "Contrôle dynamique de puissance", itemCoverHome: "Couvrir maison", itemExclusionPct: "% exclu",
    secSysLimits: "Limites de puissance du système", itemSysMaxCharge: "Charge max. système", itemSysMaxDischarge: "Décharge max. système",
    secCommon: "Contrôle commun",
    secPd: "Régulateur PD (avancé)",
    secNoPd: "Suivi direct sans PD", itemNoPdDelay: "Délai de commande",
    itemPdProfile: "Profil de réglage", itemPdQuality: "Qualité de contrôle",
    itemPdKp: "Gain proportionnel (Kp)", itemPdKd: "Gain dérivé (Kd)", itemPdDeadband: "Bande morte",
    itemPdMaxChange: "Changement de puissance max.", itemPdDirHyst: "Hystérésis de direction",
    itemPdMinCharge: "Puissance min. de charge", itemPdMinDischarge: "Puissance min. de décharge", itemPdRelayCooldown: "Temporisation relais", itemPdMinCycle: "Intervalle min. de cycle", itemPdTargetGrid: "Puissance cible réseau",
    slotSchedule: "Horaire", slotDays: "Jours", slotAll: "Toutes", slotMode: "Mode", slotManual: "Manuel", slotPd: "PD",
    slotAllows: "Autorise", slotChargeWord: "charge", slotDischargeWord: "décharge", slotNothing: "rien",
    slotSocOverride: "Surcharge SOC", slotYes: "oui", slotPowerOverride: "Surcharge puissance",
    slotStateLabel: "État", slotActiveWord: "actif", slotInactiveWord: "inactif",
  },
  nl: {
    subtitle: "Bedieningspaneel",
    live: "Live",
    tabResumen: "Overzicht", tabBaterias: "Batterijen", tabControl: "Bediening",
    moreInfo: "Geschiedenis tonen",
    zoomReset: "Alles",
    infoManufacturer: "Fabrikant", infoModel: "Model", infoSunSpecModel: "SunSpec-model", infoSoftware: "Software", infoSerial: "Serienr.", infoInverter: "Omvormer", infoPowerModule: "Vermogensmodule",
    placeholderMsg: "Deze weergave komt in een latere fase. Gebruik voorlopig het Overzicht.",
    cardFlow: "Energiestroom", cardSoc: "Systeemstatus", cardDaily: "Energie vandaag",
    cardWeekly: "Energie per week", cardPower: "Vermogen", cardSocToday: "SOC · vandaag",
    dailyOperationTitle: "Dagelijkse werking", dailyOperationDescription: "Geobserveerde en verwachte energie · 15 min",
    dailyOperationReal: "Werkelijk", dailyOperationCurrent: "Huidig", dailyOperationForecast: "Verwachting",
    dailyOperationStale: "Gegevens zijn mogelijk verouderd", dailyOperationNoData: "Geen gegevens voor dagelijkse werking",
    dailyOperationSolarWindow: "Zonnevenster", dailyOperationSolarCharge: "Laden met zonne-energie", dailyOperationGridCharge: "Laden vanaf net", dailyOperationDischarge: "Batterij ontladen", dailyOperationEnergyToBattery: "Energie naar batterij", dailyOperationEnergyFromBattery: "Energie uit batterij", dailyOperationCause: "Oorzaak", dailyOperationHourlyBalance: "Uurbalans",
    dailyOperationNotNeeded: "Laden vanaf net niet nodig", dailyOperationNoAction: "Geen actie", dailyOperationSetpoint: "Tot setpoint",
    dailyOperationDelay: "Laden vertraagd", dailyOperationUnlock: "Geschatte vrijgave: {time}", dailyOperationAxis: "kWh/15 min",
    dailyOperationSolar: "Zon", dailyOperationConsumption: "Verbruik", dailyOperationSoc: "Totale SOC", dailyOperationLearned: "Geleerd profiel",
    dailyOperationSinusoidal: "Alternatieve sinuscurve", dailyOperationGenericFallback: "Alternatieve prognose", dailyOperationZeroFallback: "Zonneprognose zonder opbrengst", dailyOperationFallback: "Alternatieve prognose: {reason}", dailyOperationPlan: "Plan",
    dailyOperationObserved: "Geobserveerd", dailyOperationProjected: "Verwacht", dailyOperationPrevious: "Vorige",
    dailyOperationNext: "Volgende", dailyOperationNow: "Nu", dailyOperationCoverage: "Dekking", dailyOperationMode: "Modus",
    dailyOperationSource: "Bron", dailyOperationDSTSkipped: "DST-interval overgeslagen", dailyOperationDSTRepeated: "Herhaalde lokale tijd",
    dailyOperationProvider: "Prognose van aanbieder", dailyOperationConsumptionProfile: "Verbruiksprofiel", dailyOperationLegacyDaily: "Dagelijkse schatting", dailyOperationDynamicSchedule: "Dynamische planning", dailyOperationTimeSlot: "Tijdvak", dailyOperationProfileProjection: "Profielprojectie", dailyOperationUnavailable: "Niet beschikbaar",
    dailyOperationRealtimeNoFuture: "Realtimeprijs heeft geen toekomstige kalender", dailyOperationUnknown: "Onbekend",
    forecastToday: "Zonneverwachting · 00:05", solarRemaining: "Resterende zonne-energie", expectedConsumption: "Verwacht verbruik",
    grid: "Net", solar: "Zon", home: "Huis", battery: "Batterij",
    excludedDevices: "Uitgesloten app.",
    importing: "Invoer", exporting: "Teruglevering",
    charging: "Laden", discharging: "Ontladen", idle: "Rust",
    selfConsumptionSuffix: "% zelfconsumptie", units: "stuks",
    charge: "Laden", discharge: "Ontladen", availOf: "van {value} beschikbaar",
    charged: "Geladen", discharged: "Ontladen",
    gridImport: "Net ingevoerd", gridExport: "Net teruggeleverd",
    now: "nu", noData: "Geen gegevens", imported: "Ingevoerd", exported: "Teruggeleverd",
    diagTitle: "Integratiestatus",
    diagIntegration: "Integratie", diagPhaseProtection: "Fasebeveiliging", diagPdState: "PD-status", diagNetBalance: "Nettosaldo", diagAlarm: "Alarm",
    diagActiveBatteries: "Actieve batterijen", diagNonResponsive: "Geen reactie",
    diagDischargeWindow: "Ontlaadvenster", diagPredictive: "Voorspellend laden", diagCurtailment: "Slim voorontladen",
    diagPeak: "Piekbegrenzing", diagWeeklyCharge: "Wekelijkse lading", diagChargeDelay: "Laadvertraging",
    nResponsive: "{n} geen reactie", none: "Geen",
    noBatteriesTitle: "Geen batterijen",
    noBatteriesMsg: "Er zijn geen batterijapparaten gedetecteerd in deze integratie.",
    healthCells: "Gezondheid & cellen",
    mTemp: "Temperatuur", mVoltage: "Spanning", mCellMax: "Cel max", mCellMin: "Cel min",
    mCellDelta: "Δ cel", mCycles: "Cycli", mEfficiency: "Efficiëntie", mHysteresis: "Hysterese",
    solarMppt: "Solar (MPPT)", controls: "Bediening", deviceInfo: "Apparaatinformatie",
    acOutput: "AC-uitgang", acInput: "AC-ingang", toHomeGrid: "Naar huis / net", fromAcBus: "Vanaf AC-bus",
    inverterMode: "Omvormer · {state}",
    offgrid: "Eilandbedrijf", infoComm: "Comm.-module",
    invBackup: "Back-up", invUpdating: "Bijwerken", invStandby: "Stand-by", invBypass: "Bypass",
    active: "Actief", inactive: "Inactief",
    ctlEmpty: "Geen bedieningen ingeschakeld. Schakel ze in op het apparaat (Instellingen → uitgeschakelde entiteiten).",
    sysEmptyTitle: "Geen bedieningen beschikbaar",
    sysEmptyMsg: "Deze integratie biedt geen systeembedieningen, of ze zijn uitgeschakeld. Schakel ze in via Instellingen → entiteiten.",
    bcAllowCharge: "Laden toestaan", bcAllowDischarge: "Ontladen toestaan", bcBatteryManual: "Handmatige batterijregeling", bcFroniusLock: "Interne Fronius/BYD-regeling uitgeschakeld houden",
    bcSocMax: "Max. SOC", bcSocMin: "Min. SOC", bcForceMode: "Geforceerde modus",
    bcChargePower: "Laadvermogen", bcDischargePower: "Ontlaadvermogen",
    bcMaxCharge: "Max. laden", bcMaxDischarge: "Max. ontladen",
    bcChargeToSoc: "Laden tot SOC", bcChargeHysteresis: "Laadhysterese", bcBackup: "Back-upfunctie", bcOffgridMode: "Off-grid-modus",
    bcBackupThreshold: "Back-updrempel", bcVoltageTaper: "100%-laadbegrenzing", bcActiveBalance: "Actieve celbalans",
    secManual: "Handmatige modus", secOffgridMeter: "Off-grid metermodus", secVacation: "Vakantiemodus", itemEnable: "Inschakelen",
    secTempLimit: "Temperatuurbegrenzing laden", itemTempLimitC: "Temperatuurlimiet", itemTempLimitBand: "Afbouwband", itemTempLimitFloor: "Minimaal laadvermogen", itemTempApplyDischarge: "Ook ontladen terugregelen",
    itemMaxContracted: "Max. gecontracteerd vermogen", itemSolarSafety: "Veiligheidsmarge zon", itemGridChargeMargin: "Netladingsmarge", itemMinSocFloorEnable: "SOC-vloer", itemMinSocFloor: "Gegarandeerde min. SOC",
    itemSocThreshold: "SOC-drempel", itemPeakLimit: "Pieklimiet", itemExcludedPeakShaving: "Piekbegrenzing voor uitgesloten apparaten",
    itemArbitrageMargin: "Min. arbitragemarge", itemRoundTripEfficiency: "Retourrendement", itemMaxPrice: "Max. prijs (laden)", itemDischargePrice: "Ontlaad-prijsondergrens", itemPriceDischarge: "Alleen ontladen bij hoge prijs", itemReevaluatePrices: "Prijzen nu herberekenen", itemNegativePriceCharging: "Laden bij negatieve prijzen", itemSmartPredischarge: "Slim voorontladen", itemNegativeThreshold: "Drempel negatieve injectie", itemPredischargeReserve: "Reserve-SOC voorontladen", itemPredischargeExport: "Exportlimiet voorontladen",
    itemDelaySafety: "Veiligheidsmarge", itemDelaySoc: "Doel-SOC vertraging", itemDelaySocEnable: "Doel-SOC vertraging actief", itemDelayDeadband: "Balans dode band",
    secHourly: "Uurbalans", hourlyEsOnly: "Alleen nuttig in Spanje (RD 244/2019) · gedetecteerd land: {c}", secWeeklyFull: "Wekelijkse volledige lading", itemWeeklyDay: "Dag volledige lading", itemWeeklyDelay: "Wachten op zonne-laadvertraging", itemHourlyTarget: "Doel nettosaldo", itemHourlyMaxOffset: "Max. vermogensoffset", itemHourlyDeadband: "Dodeband", itemHourlyHysteresis: "Hysterese",
    secSlots: "Geconfigureerde tijdvakken", itemSlot: "Tijdvak",
    secExcluded: "Uitgesloten apparaten", itemExcludedDevice: "Uitgesloten apparaat", itemSolarSurplus: "Zonne-overschot", itemDynamicPowerControl: "Dynamische vermogensregeling", itemCoverHome: "Huis dekken", itemExclusionPct: "Uitsluiting %",
    secSysLimits: "Systeemvermogenslimieten", itemSysMaxCharge: "Max. systeemladen", itemSysMaxDischarge: "Max. systeemontladen",
    secCommon: "Gemeenschappelijke regeling",
    secPd: "PD-regelaar (geavanceerd)",
    secNoPd: "Directe tracking zonder PD", itemNoPdDelay: "Commandovertraging",
    itemPdProfile: "Afstemprofiel", itemPdQuality: "Regelkwaliteit",
    itemPdKp: "Proportionele versterking (Kp)", itemPdKd: "Differentiële versterking (Kd)", itemPdDeadband: "Dode zone",
    itemPdMaxChange: "Max. vermogenswijziging", itemPdDirHyst: "Richtingshysterese",
    itemPdMinCharge: "Min. laadvermogen", itemPdMinDischarge: "Min. ontlaadvermogen", itemPdRelayCooldown: "Relais-wachttijd", itemPdMinCycle: "Min. cyclusinterval", itemPdTargetGrid: "Doelnetvermogen",
    slotSchedule: "Schema", slotDays: "Dagen", slotAll: "Alle", slotMode: "Modus", slotManual: "Handmatig", slotPd: "PD",
    slotAllows: "Staat toe", slotChargeWord: "laden", slotDischargeWord: "ontladen", slotNothing: "niets",
    slotSocOverride: "SOC-overschrijving", slotYes: "ja", slotPowerOverride: "Vermogensoverschrijving",
    slotStateLabel: "Status", slotActiveWord: "actief", slotInactiveWord: "inactief",
  },
};

// Backend diagnostics stay stable and machine-readable. Translate them only at
// the presentation boundary so entities and downloaded diagnostics keep their
// existing contract while the panel never exposes internal reason codes.
const DAILY_OPERATION_REASON_KEYS = {
  no_profile_data: "profileNoData",
  insufficient_days: "profileInsufficientDays",
  insufficient_weekday_samples: "profileInsufficientWeekday",
  insufficient_coverage: "profileInsufficientCoverage",
  stale_profile: "profileStale",
  profile_not_mature: "profileLearning",
  empty_range: "emptyRange",
  empty_horizon: "emptyRange",
  provider_period_invalid: "providerInvalid",
  provider_invalid: "providerInvalid",
  provider_timestamp_naive: "providerNoTimezone",
  provider_periods_missing: "providerMissing",
  provider_period_overlap: "providerOverlap",
  provider_coverage: "providerCoverage",
  provider_gap: "providerGap",
  provider_zero_energy: "providerNoEnergy",
  legacy_shape_missing: "legacyMissing",
  legacy_shape_length: "legacyInvalid",
  legacy_shape_invalid: "legacyInvalid",
  legacy_shape_zero: "legacyInvalid",
  solar_window_missing: "solarWindowMissing",
  solar_window_invalid: "solarWindowInvalid",
  learned_shape_length: "learnedInvalid",
  learned_shape_invalid: "learnedInvalid",
  learned_invalid: "learnedInvalid",
  learned_shape_no_future_energy: "learnedNoFuture",
  invalid_mode: "invalidMode",
  sinusoidal_invalid: "sinusoidalInvalid",
  forecast_invalid: "forecastInvalid",
  unsafe_temporal_shape: "temporalInvalid",
};

const DAILY_OPERATION_REASON_I18N = {
  en: {
    profileNoData: "No data for the learned profile",
    profileInsufficientDays: "Not enough learned days",
    profileInsufficientWeekday: "Not enough samples for this weekday",
    profileInsufficientCoverage: "Insufficient profile coverage",
    profileStale: "The learned profile is outdated",
    profileLearning: "The profile is still learning",
    emptyRange: "No future intervals are available for the forecast",
    providerInvalid: "The provider forecast is invalid",
    providerNoTimezone: "The provider forecast has no time zone",
    providerMissing: "No provider forecast intervals are available",
    providerOverlap: "The provider forecast intervals overlap",
    providerCoverage: "The provider forecast coverage is insufficient",
    providerGap: "The provider forecast contains a data gap",
    providerNoEnergy: "The provider forecast contains no solar energy",
    legacyMissing: "The previous solar curve is unavailable",
    legacyInvalid: "The previous solar curve is invalid",
    solarWindowMissing: "The solar window is unavailable",
    solarWindowInvalid: "The solar window is invalid",
    learnedInvalid: "The learned solar profile is invalid",
    learnedNoFuture: "The learned profile contains no future solar energy",
    invalidMode: "The solar profile mode is invalid",
    sinusoidalInvalid: "The sinusoidal solar curve could not be generated",
    forecastInvalid: "The solar forecast is invalid",
    temporalInvalid: "The solar time distribution could not be validated",
    normalizationFailed: "The solar forecast could not be distributed",
    loadFailed: "The profile data could not be loaded",
    saveFailed: "The profile data could not be saved",
    historyUnavailable: "The profile history could not be imported",
    profileReset: "The profile was reset after a configuration change",
    fallbackUnavailable: "The fallback estimate is unavailable",
    projectionUnavailable: "The projection is unavailable",
    projectionFailed: "The projection could not be calculated",
    runtimeFailed: "The live data could not be updated",
    updateFailed: "The chart could not be updated",
  },
  es: {
    profileNoData: "Sin datos para el perfil aprendido",
    profileInsufficientDays: "No hay suficientes días de aprendizaje",
    profileInsufficientWeekday: "No hay suficientes muestras para este día de la semana",
    profileInsufficientCoverage: "Cobertura insuficiente del perfil",
    profileStale: "El perfil aprendido está desactualizado",
    profileLearning: "El perfil todavía está aprendiendo",
    emptyRange: "No hay intervalos futuros para la previsión",
    providerInvalid: "La previsión del proveedor no es válida",
    providerNoTimezone: "La previsión del proveedor no incluye zona horaria",
    providerMissing: "No hay intervalos de previsión del proveedor",
    providerOverlap: "Los intervalos de previsión del proveedor se solapan",
    providerCoverage: "La cobertura de la previsión del proveedor es insuficiente",
    providerGap: "La previsión del proveedor contiene un intervalo sin datos",
    providerNoEnergy: "La previsión del proveedor no contiene energía solar",
    legacyMissing: "La curva solar anterior no está disponible",
    legacyInvalid: "La curva solar anterior no es válida",
    solarWindowMissing: "La ventana solar no está disponible",
    solarWindowInvalid: "La ventana solar no es válida",
    learnedInvalid: "El perfil solar aprendido no es válido",
    learnedNoFuture: "El perfil aprendido no contiene energía solar futura",
    invalidMode: "El modo del perfil solar no es válido",
    sinusoidalInvalid: "No se pudo generar la curva solar sinusoidal",
    forecastInvalid: "La previsión solar no es válida",
    temporalInvalid: "No se pudo validar la distribución temporal solar",
    normalizationFailed: "No se pudo distribuir la previsión solar",
    loadFailed: "No se pudieron cargar los datos del perfil",
    saveFailed: "No se pudieron guardar los datos del perfil",
    historyUnavailable: "No se pudo importar el histórico del perfil",
    profileReset: "El perfil se reinició tras un cambio de configuración",
    fallbackUnavailable: "La estimación alternativa no está disponible",
    projectionUnavailable: "La proyección no está disponible",
    projectionFailed: "No se pudo calcular la proyección",
    runtimeFailed: "No se pudieron actualizar los datos en tiempo real",
    updateFailed: "No se pudo actualizar la gráfica",
  },
  ca: {
    profileNoData: "Sense dades per al perfil après",
    profileInsufficientDays: "No hi ha prou dies d'aprenentatge",
    profileInsufficientWeekday: "No hi ha prou mostres per a aquest dia de la setmana",
    profileInsufficientCoverage: "Cobertura insuficient del perfil",
    profileStale: "El perfil après està desactualitzat",
    profileLearning: "El perfil encara està aprenent",
    emptyRange: "No hi ha intervals futurs per a la previsió",
    providerInvalid: "La previsió del proveïdor no és vàlida",
    providerNoTimezone: "La previsió del proveïdor no inclou zona horària",
    providerMissing: "No hi ha intervals de previsió del proveïdor",
    providerOverlap: "Els intervals de previsió del proveïdor se superposen",
    providerCoverage: "La cobertura de la previsió del proveïdor és insuficient",
    providerGap: "La previsió del proveïdor conté un interval sense dades",
    providerNoEnergy: "La previsió del proveïdor no conté energia solar",
    legacyMissing: "La corba solar anterior no està disponible",
    legacyInvalid: "La corba solar anterior no és vàlida",
    solarWindowMissing: "La finestra solar no està disponible",
    solarWindowInvalid: "La finestra solar no és vàlida",
    learnedInvalid: "El perfil solar après no és vàlid",
    learnedNoFuture: "El perfil après no conté energia solar futura",
    invalidMode: "El mode del perfil solar no és vàlid",
    sinusoidalInvalid: "No s'ha pogut generar la corba solar sinusoidal",
    forecastInvalid: "La previsió solar no és vàlida",
    temporalInvalid: "No s'ha pogut validar la distribució temporal solar",
    normalizationFailed: "No s'ha pogut distribuir la previsió solar",
    loadFailed: "No s'han pogut carregar les dades del perfil",
    saveFailed: "No s'han pogut desar les dades del perfil",
    historyUnavailable: "No s'ha pogut importar l'històric del perfil",
    profileReset: "El perfil s'ha reiniciat després d'un canvi de configuració",
    fallbackUnavailable: "L'estimació alternativa no està disponible",
    projectionUnavailable: "La projecció no està disponible",
    projectionFailed: "No s'ha pogut calcular la projecció",
    runtimeFailed: "No s'han pogut actualitzar les dades en temps real",
    updateFailed: "No s'ha pogut actualitzar la gràfica",
  },
  de: {
    profileNoData: "Keine Daten für das gelernte Profil",
    profileInsufficientDays: "Nicht genügend Lerntage",
    profileInsufficientWeekday: "Nicht genügend Messwerte für diesen Wochentag",
    profileInsufficientCoverage: "Unzureichende Profilabdeckung",
    profileStale: "Das gelernte Profil ist veraltet",
    profileLearning: "Das Profil wird noch angelernt",
    emptyRange: "Für die Prognose sind keine zukünftigen Intervalle verfügbar",
    providerInvalid: "Die Anbieterprognose ist ungültig",
    providerNoTimezone: "Die Anbieterprognose enthält keine Zeitzone",
    providerMissing: "Es sind keine Prognoseintervalle des Anbieters verfügbar",
    providerOverlap: "Die Prognoseintervalle des Anbieters überschneiden sich",
    providerCoverage: "Die Abdeckung der Anbieterprognose ist unzureichend",
    providerGap: "Die Anbieterprognose weist eine Datenlücke auf",
    providerNoEnergy: "Die Anbieterprognose enthält keine Solarenergie",
    legacyMissing: "Die bisherige Solarkurve ist nicht verfügbar",
    legacyInvalid: "Die bisherige Solarkurve ist ungültig",
    solarWindowMissing: "Das Solarzeitfenster ist nicht verfügbar",
    solarWindowInvalid: "Das Solarzeitfenster ist ungültig",
    learnedInvalid: "Das gelernte Solarprofil ist ungültig",
    learnedNoFuture: "Das gelernte Profil enthält keine zukünftige Solarenergie",
    invalidMode: "Der Solarprofilmodus ist ungültig",
    sinusoidalInvalid: "Die sinusförmige Solarkurve konnte nicht erstellt werden",
    forecastInvalid: "Die Solarprognose ist ungültig",
    temporalInvalid: "Die zeitliche Solarverteilung konnte nicht validiert werden",
    normalizationFailed: "Die Solarprognose konnte nicht verteilt werden",
    loadFailed: "Die Profildaten konnten nicht geladen werden",
    saveFailed: "Die Profildaten konnten nicht gespeichert werden",
    historyUnavailable: "Der Profilverlauf konnte nicht importiert werden",
    profileReset: "Das Profil wurde nach einer Konfigurationsänderung zurückgesetzt",
    fallbackUnavailable: "Die Ersatzprognose ist nicht verfügbar",
    projectionUnavailable: "Die Projektion ist nicht verfügbar",
    projectionFailed: "Die Projektion konnte nicht berechnet werden",
    runtimeFailed: "Die Live-Daten konnten nicht aktualisiert werden",
    updateFailed: "Das Diagramm konnte nicht aktualisiert werden",
  },
  fr: {
    profileNoData: "Aucune donnée pour le profil appris",
    profileInsufficientDays: "Pas assez de jours d'apprentissage",
    profileInsufficientWeekday: "Pas assez d'échantillons pour ce jour de la semaine",
    profileInsufficientCoverage: "Couverture du profil insuffisante",
    profileStale: "Le profil appris est obsolète",
    profileLearning: "Le profil est encore en cours d'apprentissage",
    emptyRange: "Aucun intervalle futur n'est disponible pour la prévision",
    providerInvalid: "La prévision du fournisseur n'est pas valide",
    providerNoTimezone: "La prévision du fournisseur ne contient pas de fuseau horaire",
    providerMissing: "Aucun intervalle de prévision du fournisseur n'est disponible",
    providerOverlap: "Les intervalles de prévision du fournisseur se chevauchent",
    providerCoverage: "La couverture de la prévision du fournisseur est insuffisante",
    providerGap: "La prévision du fournisseur contient un intervalle sans données",
    providerNoEnergy: "La prévision du fournisseur ne contient aucune énergie solaire",
    legacyMissing: "La courbe solaire précédente n'est pas disponible",
    legacyInvalid: "La courbe solaire précédente n'est pas valide",
    solarWindowMissing: "La fenêtre solaire n'est pas disponible",
    solarWindowInvalid: "La fenêtre solaire n'est pas valide",
    learnedInvalid: "Le profil solaire appris n'est pas valide",
    learnedNoFuture: "Le profil appris ne contient aucune énergie solaire future",
    invalidMode: "Le mode du profil solaire n'est pas valide",
    sinusoidalInvalid: "La courbe solaire sinusoïdale n'a pas pu être générée",
    forecastInvalid: "La prévision solaire n'est pas valide",
    temporalInvalid: "La répartition temporelle solaire n'a pas pu être validée",
    normalizationFailed: "La prévision solaire n'a pas pu être répartie",
    loadFailed: "Les données du profil n'ont pas pu être chargées",
    saveFailed: "Les données du profil n'ont pas pu être enregistrées",
    historyUnavailable: "L'historique du profil n'a pas pu être importé",
    profileReset: "Le profil a été réinitialisé après un changement de configuration",
    fallbackUnavailable: "L'estimation de secours n'est pas disponible",
    projectionUnavailable: "La projection n'est pas disponible",
    projectionFailed: "La projection n'a pas pu être calculée",
    runtimeFailed: "Les données en temps réel n'ont pas pu être actualisées",
    updateFailed: "Le graphique n'a pas pu être actualisé",
  },
  nl: {
    profileNoData: "Geen gegevens voor het geleerde profiel",
    profileInsufficientDays: "Onvoldoende leerdagen",
    profileInsufficientWeekday: "Onvoldoende metingen voor deze weekdag",
    profileInsufficientCoverage: "Onvoldoende profieldekking",
    profileStale: "Het geleerde profiel is verouderd",
    profileLearning: "Het profiel is nog aan het leren",
    emptyRange: "Er zijn geen toekomstige intervallen beschikbaar voor de prognose",
    providerInvalid: "De prognose van de aanbieder is ongeldig",
    providerNoTimezone: "De prognose van de aanbieder bevat geen tijdzone",
    providerMissing: "Er zijn geen prognose-intervallen van de aanbieder beschikbaar",
    providerOverlap: "De prognose-intervallen van de aanbieder overlappen",
    providerCoverage: "De dekking van de prognose van de aanbieder is onvoldoende",
    providerGap: "De prognose van de aanbieder bevat een gegevenshiaat",
    providerNoEnergy: "De prognose van de aanbieder bevat geen zonne-energie",
    legacyMissing: "De vorige zonnecurve is niet beschikbaar",
    legacyInvalid: "De vorige zonnecurve is ongeldig",
    solarWindowMissing: "Het zonnevenster is niet beschikbaar",
    solarWindowInvalid: "Het zonnevenster is ongeldig",
    learnedInvalid: "Het geleerde zonneprofiel is ongeldig",
    learnedNoFuture: "Het geleerde profiel bevat geen toekomstige zonne-energie",
    invalidMode: "De zonneprofielmodus is ongeldig",
    sinusoidalInvalid: "De sinusvormige zonnecurve kon niet worden gegenereerd",
    forecastInvalid: "De zonneprognose is ongeldig",
    temporalInvalid: "De verdeling van zonne-energie over de tijd kon niet worden gevalideerd",
    normalizationFailed: "De zonneprognose kon niet worden verdeeld",
    loadFailed: "De profielgegevens konden niet worden geladen",
    saveFailed: "De profielgegevens konden niet worden opgeslagen",
    historyUnavailable: "De profielgeschiedenis kon niet worden geïmporteerd",
    profileReset: "Het profiel is opnieuw ingesteld na een configuratiewijziging",
    fallbackUnavailable: "De alternatieve prognose is niet beschikbaar",
    projectionUnavailable: "De projectie is niet beschikbaar",
    projectionFailed: "De projectie kon niet worden berekend",
    runtimeFailed: "De livegegevens konden niet worden bijgewerkt",
    updateFailed: "De grafiek kon niet worden bijgewerkt",
  },
};

// translation_key -> role. These are stable identifiers set by the integration
// (see const.py / *_sensors.py), independent of the user's language or renames.
const K = {
  // per battery
  batterySoc: "battery_soc",
  acPower: "ac_power", // AC-side power. HA sign: - charge / + discharge (W)
  batteryPower: "battery_power", // synthesised cell power (Zendure). + charge / - discharge (W)
  batteryCellPower: "battery_cell_power", // Venus A/D net cell power. + charge / - discharge (W)
  solarPower: "solar_power", // aggregate DC PV power (W)
  acOffgridPower: "ac_offgrid_power", // off-grid/backup AC output. HA sign: + discharge (W)
  storedEnergy: "stored_energy", // kWh
  batteryTotalEnergy: "battery_total_energy", // capacity kWh
  inverterState: "inverter_state",
  dailyCharge: "total_daily_charging_energy",
  dailyDischarge: "total_daily_discharging_energy",
  maxChargePower: "max_charge_power",
  maxDischargePower: "max_discharge_power",
  inverseMaxPower: "inverse_max_power", // Zendure discharge cap (shares the bcMaxDischarge label)
  batteryVoltage: "battery_voltage",
  internalTemp: "internal_temperature",
  cellMax: "max_cell_voltage",
  cellMin: "min_cell_voltage",
  cellDelta: "cell_delta", // measured imbalance (mV) from the balance monitor
  cycles: "battery_cycle_count",
  cyclesCalc: "battery_cycle_count_calc",
  rte: "round_trip_efficiency_total",
  softwareVersion: "software_version",
  froniusSunspecModelType: "fronius_sunspec_model_type",
  powerModuleSerial: "power_module_serial_number",
  powerModuleFirmware: "power_module_firmware_version",
  inverterSerial: "inverter_serial_number",
  inverterFirmware: "inverter_software_version",
  pack1Firmware: "pack1_firmware_version",
  pack2Firmware: "pack2_firmware_version",
  pack3Firmware: "pack3_firmware_version",
  pack1Serial: "pack1_serial_number",
  pack2Serial: "pack2_serial_number",
  pack3Serial: "pack3_serial_number",
  bmsVersion: "bms_version",
  vmsVersion: "vms_version",
  emsVersion: "ems_version",
  commFw: "comm_module_firmware",
  wifiSignal: "wifi_signal_strength",
  wifiStatus: "wifi_status",
  mac: "mac_address",
  deviceName: "device_name",
  // backup / offgrid + charge hysteresis (per battery)
  acOffgridPower: "ac_offgrid_power", // power delivered to off-grid/backup loads (W)
  backupFunction: "backup_function", // backup/off-grid switch
  chargeHysteresisActive: "charge_hysteresis", // binary: hysteresis blocking charge
  // system aggregates
  sysSoc: "system_soc",
  sysStored: "system_stored_energy",
  sysCapacity: "system_total_energy",
  sysChargePower: "system_charge_power",
  sysDischargePower: "system_discharge_power",
  sysBattCellPower: "system_battery_cell_power", // signed net battery power (+charge/-discharge); always present

  sysHomePower: "system_home_consumption", // derived instantaneous home consumption (W)
  sysDailyCharge: "system_daily_charging_energy",
  sysDailyDischarge: "system_daily_discharging_energy",
  sysDailySolar: "system_daily_solar_energy", // exact daily PV production (kWh)
  sysDailyHome: "system_daily_home_energy", // exact daily home consumption (kWh)
  sysDailyGridImport: "system_daily_grid_import_energy", // exact daily grid import (kWh)
  sysDailyGridExport: "system_daily_grid_export_energy", // exact daily grid export (kWh)
  consumptionProfile: "expected_home_consumption_profile",
  sysAlarm: "system_alarm_status",
  pdQuality: "system_pd_control_quality", // PD control-quality verdict
  // diagnostics / flags
  netBalance: "balance_neto",
  activeBatteries: "active_batteries",
  nonResponsive: "non_responsive_batteries",
  integration: "integration_status",
  phaseProtection: "three_phase_protection_status",
  dischargeWindow: "discharge_window",
  predictiveSwitch: "predictive_charging",
  peakSwitch: "capacity_protection",
  // diagnostic-category entities of the "Marstek Venus System" device
  predictiveActive: "predictive_charging_active",
  curtailmentActive: "curtailment_status",
  capacityActive: "capacity_protection_active",
  weeklyFullCharge: "weekly_full_charge",
  chargeDelay: "charge_delay_status",
};

const MPPT_KEYS = ["mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power"];

// Diagnostic rows shown in the SOC card's second section (2-column grid).
// One per diagnostic-category entity on the system device, except balance_neto
// (own dedicated card).
// Values are localized at render time via hass.formatEntityState.
const DIAG_ROWS = [
  { key: K.integration, lk: "diagIntegration" },
  { key: K.phaseProtection, lk: "diagPhaseProtection" },
  { key: K.sysAlarm, lk: "diagAlarm" },
  { key: K.activeBatteries, lk: "diagActiveBatteries" },
  { key: K.nonResponsive, lk: "diagNonResponsive" },
  { key: K.dischargeWindow, lk: "diagDischargeWindow" },
  { key: K.predictiveActive, lk: "diagPredictive" },
  { key: K.curtailmentActive, lk: "diagCurtailment" },
  { key: K.chargeDelay, lk: "diagChargeDelay" },
  { key: K.weeklyFullCharge, lk: "diagWeeklyCharge" },
  { key: K.capacityActive, lk: "diagPeak" },
  { key: K.netBalance, lk: "diagNetBalance" },
  { key: K.pdQuality, lk: "diagPdState" },
];

// Cell-imbalance color thresholds (raw delta, mV). Mirror const.py
// BALANCE_THRESHOLD_YELLOW/ORANGE/RED so the panel tier matches the integration.
const DELTA_MV_YELLOW = 200;
const DELTA_MV_ORANGE = 230;
const DELTA_MV_RED = 250;

// Per-battery control entities, matched by translation_key. A control is only
// rendered when its entity is enabled (has a live state); most default to
// disabled in the integration. `domain` selects the widget + service.
const BAT_CONTROLS = [
  { key: "battery_allow_charge", domain: "switch", lk: "bcAllowCharge", icon: "mdi:battery-arrow-up" },
  { key: "battery_allow_discharge", domain: "switch", lk: "bcAllowDischarge", icon: "mdi:battery-arrow-down" },
  { key: "battery_manual_mode", domain: "switch", lk: "bcBatteryManual", icon: "mdi:hand-back-right-outline" },
  { key: "fronius_internal_control_disabled", domain: "switch", lk: "bcFroniusLock", icon: "mdi:battery-lock" },
  // SOC limits: the Marstek register and its Zendure equivalent share each label;
  // only one of each pair exists on a given device, so both layouts read
  // "SOC máximo" then "SOC mínimo" in this order.
  { key: "charging_cutoff_capacity", domain: "number", lk: "bcSocMax", icon: "mdi:battery-high" },
  { key: "soc_set", domain: "number", lk: "bcSocMax", icon: "mdi:battery-high" },
  { key: "discharging_cutoff_capacity", domain: "number", lk: "bcSocMin", icon: "mdi:battery-low" },
  { key: "min_soc", domain: "number", lk: "bcSocMin", icon: "mdi:battery-low" },
  { key: "force_mode", domain: "select", lk: "bcForceMode", icon: "mdi:gesture-tap-button" },
  { key: "set_charge_power", domain: "number", lk: "bcChargePower", icon: "mdi:battery-arrow-up-outline" },
  { key: "set_discharge_power", domain: "number", lk: "bcDischargePower", icon: "mdi:battery-arrow-down-outline" },
  { key: "max_charge_power", domain: "number", lk: "bcMaxCharge", icon: "mdi:battery-arrow-up-outline" },
  // Max discharge: Marstek register + Zendure inverter-output cap share the label.
  { key: "max_discharge_power", domain: "number", lk: "bcMaxDischarge", icon: "mdi:battery-arrow-down-outline" },
  { key: "inverse_max_power", domain: "number", lk: "bcMaxDischarge", icon: "mdi:battery-arrow-down-outline" },
  { key: "charge_to_soc", domain: "number", lk: "bcChargeToSoc", icon: "mdi:battery-sync-outline" },
  { key: "charge_hysteresis_percent", domain: "number", lk: "bcChargeHysteresis", icon: "mdi:battery-sync" },
  { key: "backup_function", domain: "switch", lk: "bcBackup", icon: "mdi:home-battery-outline" },
  // Offgrid load threshold for the backup output (software-only, no register).
  { key: "backup_offgrid_threshold", domain: "number", lk: "bcBackupThreshold", icon: "mdi:transmission-tower-off" },
  { key: "battery_phase", domain: "select", lk: "bcBatteryPhase", icon: "mdi:transmission-tower" },
  // Zendure off-grid output port mode (select: normal/economy/off). Distinct from
  // the Marstek backup_function switch; only one exists per device.
  { key: "grid_off_mode", domain: "select", lk: "bcOffgridMode", icon: "mdi:transmission-tower-off" },
  // Cell-maintenance switch (Marstek only): 100% charge voltage taper.
  { key: "full_charge_voltage_taper", domain: "switch", lk: "bcVoltageTaper", icon: "mdi:battery-clock" },
];

// Unified Control tab: system-level entities grouped BY FEATURE — each section
// is one capability with its on/off switch first, then its related config
// params (CONFIG number sliders / selects). Entities are matched by
// translation_key on the system device (switch.py/select.py/number.py with
// identifier "marstek_venus_system"). domain defaults to "number". Only entities
// with a live state render; conditional params only exist when their feature is
// configured, so a section collapses to just what's present (and hides if empty).
// `tk`/`lk` are i18n keys resolved at render time (see _t). labelFn/titleFn
// receive the live state and a translator `t` so dynamic text is localized too.
const SYS_SECTIONS = [
  {
    tk: "secPhaseProtection",
    icon: "mdi:shield-check-outline",
    items: [
      { key: "three_phase_protection", domain: "switch", lk: "threePhaseProtection", icon: "mdi:shield-check-outline" },
    ],
  },
  {
    tk: "secManual",
    icon: "mdi:hand-back-right-outline",
    items: [
      { key: "manual_mode", domain: "switch", lk: "secManual", icon: "mdi:hand-back-right-outline" },
    ],
  },
  {
    tk: "secOffgridMeter",
    icon: "mdi:transmission-tower-off",
    items: [
      { key: "offgrid_mode", domain: "switch", lk: "secOffgridMeter", icon: "mdi:transmission-tower-off" },
    ],
  },
  {
    tk: "secWeeklyFull",
    icon: "mdi:calendar-check",
    items: [
      { key: "weekly_full_charge_enabled", domain: "switch", lk: "itemEnable", icon: "mdi:calendar-check", gate: true },
      { key: "weekly_full_charge_day", domain: "select", lk: "itemWeeklyDay", icon: "mdi:calendar-week" },
      { key: "weekly_full_charge_delay", domain: "switch", lk: "itemWeeklyDelay", icon: "mdi:timer-sand" },
    ],
  },
  {
    tk: "secSlots",
    icon: "mdi:calendar-clock",
    // time_slot is indexed (one per slot). Label is the short "Slot N"; the
    // slot's details (schedule/days/apply-to-charge/state) go in a hover tooltip.
    items: [
      {
        key: "time_slot",
        domain: "switch",
        lk: "itemSlot",
        icon: "mdi:calendar-clock",
        labelFn: (st, t) => {
          const a = (st && st.attributes) || {};
          const m = String(a.friendly_name || "").match(/(\d+)\s*$/);
          return m ? `${t("itemSlot")} ${m[1]}` : null;
        },
        titleFn: (st, t) => {
          const a = (st && st.attributes) || {};
          const names = a.battery_names || {};
          const lim = a.battery_limits || {};
          const L = [];
          if (a.schedule && a.schedule !== "??-??") L.push(`${t("slotSchedule")}: ${a.schedule}`);
          if (a.days && a.days !== "None") L.push(`${t("slotDays")}: ${a.days}`);
          L.push(`${t("tabBaterias")}: ${a.battery_scope === "all" || !a.battery_scope_name ? t("slotAll") : a.battery_scope_name}`);
          if (a.mode) L.push(`${t("slotMode")}: ${a.mode === "manual" ? t("slotManual") : t("slotPd")}`);
          const allow = [];
          if (a.allow_charge) allow.push(t("slotChargeWord"));
          if (a.allow_discharge) allow.push(t("slotDischargeWord"));
          L.push(`${t("slotAllows")}: ${allow.length ? allow.join(" + ") : t("slotNothing")}`);
          if (a.soc_override_enabled) {
            const p = Object.entries(lim)
              .filter(([, v]) => v && (v.soc_min != null || v.soc_max != null))
              .map(([k, v]) => `${names[k] || k} ${v.soc_min ?? "—"}–${v.soc_max ?? "—"}%`);
            L.push(`${t("slotSocOverride")}: ${p.length ? p.join(", ") : t("slotYes")}`);
          }
          if (a.power_override_enabled) {
            const p = Object.entries(lim)
              .filter(([, v]) => v && (v.max_charge_power_w != null || v.max_discharge_power_w != null))
              .map(([k, v]) => `${names[k] || k} ↑${v.max_charge_power_w ?? "—"}W ↓${v.max_discharge_power_w ?? "—"}W`);
            L.push(`${t("slotPowerOverride")}: ${p.length ? p.join(", ") : t("slotYes")}`);
          }
          L.push(`${t("slotStateLabel")}: ${st && st.state === "on" ? t("slotActiveWord") : t("slotInactiveWord")}`);
          return L.join("\n");
        },
      },
    ],
  },
  {
    tk: "secExcluded",
    icon: "mdi:power-plug-off-outline",
    // Each control is indexed per excluded device; the entity name embeds the
    // device ("{device} – Enabled" / "– Solar Surplus"), so always use it —
    // otherwise a single excluded device would show a generic, unidentifiable row.
    items: [
      { key: "excluded_device_enabled", domain: "switch", lk: "itemExcludedDevice", icon: "mdi:power-plug-off", useName: true },
      { key: "excluded_device_solar_surplus", domain: "switch", lk: "itemSolarSurplus", icon: "mdi:solar-power", useName: true },
      { key: "excluded_device_dynamic_power_control", domain: "switch", lk: "itemDynamicPowerControl", icon: "mdi:ev-station", useName: true },
      { key: "excluded_device_cover_home", domain: "switch", lk: "itemCoverHome", icon: "mdi:home-lightning-bolt", useName: true },
      { key: "excluded_device_exclusion_pct", domain: "number", lk: "itemExclusionPct", icon: "mdi:battery-charging-50", useName: true },
    ],
  },
  {
    // Knobs shared by both PD and No-PD direct tracking (kept out of the PD
    // section so it's clear they apply regardless of the active control mode).
    tk: "secCommon",
    icon: "mdi:tune-vertical",
    items: [
      { key: "vacation_mode", domain: "switch", lk: "secVacation", icon: "mdi:palm-tree" },
      { key: "pd_controller_deadband", lk: "itemPdDeadband", icon: "mdi:arrow-collapse-horizontal" },
      { key: "pd_min_charge_power", lk: "itemPdMinCharge", icon: "mdi:battery-charging-low" },
      { key: "pd_min_discharge_power", lk: "itemPdMinDischarge", icon: "mdi:battery-low" },
      { key: "pd_relay_cooldown", lk: "itemPdRelayCooldown", icon: "mdi:timer-cog-outline" },
      { key: "pd_target_grid_power", lk: "itemPdTargetGrid", icon: "mdi:transmission-tower-export" },
      { key: "max_contracted_power", lk: "itemMaxContracted", icon: "mdi:transmission-tower" },
    ],
  },
  {
    tk: "secPd",
    icon: "mdi:tune",
    items: [
      // Inverted gate: ON = PD active (no_pd_mode OFF). Toggling it flips the same
      // no_pd_mode switch, so PD and No-PD are mutually exclusive — enabling one
      // collapses the other's params.
      { key: "no_pd_mode", domain: "switch", lk: "itemPdEnable", icon: "mdi:tune", gate: true, gateInvert: true },
      { key: "pd_tuning_profile", domain: "select", lk: "itemPdProfile", icon: "mdi:tune-variant" },
      { key: "system_pd_control_quality", domain: "sensor", lk: "itemPdQuality", icon: "mdi:gauge" },
      { key: "pd_controller_kp", lk: "itemPdKp", icon: "mdi:tune" },
      { key: "pd_controller_kd", lk: "itemPdKd", icon: "mdi:tune" },
      { key: "pd_controller_max_power_change", lk: "itemPdMaxChange", icon: "mdi:delta" },
      { key: "pd_controller_direction_hysteresis", lk: "itemPdDirHyst", icon: "mdi:swap-horizontal" },
      { key: "pd_min_cycle_interval", lk: "itemPdMinCycle", icon: "mdi:timer-pause-outline" },
    ],
  },
  {
    tk: "secNoPd",
    icon: "mdi:vector-line",
    items: [
      { key: "no_pd_mode", domain: "switch", lk: "secNoPd", icon: "mdi:vector-line", gate: true },
      { key: "no_pd_command_delay", lk: "itemNoPdDelay", icon: "mdi:timer-sand" },
    ],
  },
  {
    tk: "diagPredictive",
    icon: "mdi:brain",
    items: [
      { key: "predictive_charging", domain: "switch", lk: "itemEnable", icon: "mdi:brain", gate: true },
      { key: "predictive_safety_margin_kwh", lk: "itemSolarSafety", icon: "mdi:solar-power-variant" },
      { key: "predictive_grid_charge_margin_pct", lk: "itemGridChargeMargin", icon: "mdi:transmission-tower-import" },
      { key: "min_soc_floor_enabled", domain: "switch", lk: "itemMinSocFloorEnable", icon: "mdi:battery-arrow-up" },
      { key: "predictive_min_soc_floor", lk: "itemMinSocFloor", icon: "mdi:battery-arrow-up" },
      // Pricing controls: their entities only exist when the predictive mode is
      // price-based (dp/rt switch; thresholds are dynamic-pricing only), so on
      // time-slot installs these rows simply don't render.
      { key: "dp_price_discharge_control", domain: "switch", lk: "itemPriceDischarge", icon: "mdi:cash-clock" },
      { key: "rt_price_discharge_control", domain: "switch", lk: "itemPriceDischarge", icon: "mdi:cash-clock" },
      { key: "max_price_threshold", lk: "itemMaxPrice", icon: "mdi:cash-plus" },
      { key: "discharge_price_threshold", lk: "itemDischargePrice", icon: "mdi:cash-minus" },
      { key: "min_arbitrage_margin", lk: "itemArbitrageMargin", icon: "mdi:scale-balance" },
      { key: "round_trip_efficiency", lk: "itemRoundTripEfficiency", icon: "mdi:battery-sync" },
      { key: "negative_price_charging", domain: "switch", lk: "itemNegativePriceCharging", icon: "mdi:battery-charging-100" },
      { key: "smart_predischarge", domain: "switch", lk: "itemSmartPredischarge", icon: "mdi:battery-arrow-down-outline" },
      { key: "negative_injection_threshold", lk: "itemNegativeThreshold", icon: "mdi:cash-minus" },
      { key: "predischarge_reserve_soc", lk: "itemPredischargeReserve", icon: "mdi:battery-lock" },
      { key: "predischarge_max_export_power_w", lk: "itemPredischargeExport", icon: "mdi:transmission-tower-export" },
      { key: "curtailment_status", domain: "binary_sensor", lk: "diagCurtailment", icon: "mdi:solar-power-variant" },
      // Dynamic-pricing only (system button exists solely in that mode), so on
      // time-slot / real-time installs this row simply doesn't render.
      { key: "reevaluate_dynamic_pricing", domain: "button", lk: "itemReevaluatePrices", icon: "mdi:calendar-refresh" },
    ],
  },
  {
    tk: "diagChargeDelay",
    icon: "mdi:timer-sand",
    items: [
      { key: "charge_delay", domain: "switch", lk: "itemEnable", icon: "mdi:timer-sand", gate: true },
      { key: "delay_safety_margin_min", lk: "itemDelaySafety", icon: "mdi:timer-sand-complete" },
      { key: "charge_delay_balance_deadband_kwh", lk: "itemDelayDeadband", icon: "mdi:arrow-collapse-horizontal" },
      { key: "delay_soc_setpoint_enabled", domain: "switch", lk: "itemDelaySocEnable", icon: "mdi:battery-charging-50" },
      { key: "delay_soc_setpoint", lk: "itemDelaySoc", icon: "mdi:battery-charging-50" },
    ],
  },
  {
    tk: "secHourly",
    icon: "mdi:scale-balance",
    items: [
      { key: "hourly_balance", domain: "switch", lk: "itemEnable", icon: "mdi:scale-balance", gate: true },
      { key: "hourly_balance_target_net_wh", lk: "itemHourlyTarget", icon: "mdi:scale-balance" },
      { key: "hourly_balance_max_offset_w", lk: "itemHourlyMaxOffset", icon: "mdi:arrow-expand-vertical" },
      { key: "hourly_balance_deadband_wh", lk: "itemHourlyDeadband", icon: "mdi:arrow-collapse-horizontal" },
      { key: "hourly_balance_hysteresis_w", lk: "itemHourlyHysteresis", icon: "mdi:swap-horizontal" },
    ],
  },
  {
    tk: "secSysLimits",
    icon: "mdi:speedometer",
    items: [
      { key: "system_power_limits", domain: "switch", lk: "itemEnable", icon: "mdi:speedometer", gate: true },
      { key: "system_max_charge_power", lk: "itemSysMaxCharge", icon: "mdi:battery-arrow-up-outline" },
      { key: "system_max_discharge_power", lk: "itemSysMaxDischarge", icon: "mdi:battery-arrow-down-outline" },
    ],
  },
  {
    tk: "diagPeak",
    icon: "mdi:flash-alert",
    items: [
      { key: "capacity_protection", domain: "switch", lk: "itemEnable", icon: "mdi:flash-alert", gate: true },
      { key: "capacity_protection_excluded_devices", domain: "switch", lk: "itemExcludedPeakShaving", icon: "mdi:transmission-tower-import" },
      { key: "capacity_protection_soc_threshold", lk: "itemSocThreshold", icon: "mdi:battery-alert-variant-outline" },
      { key: "capacity_protection_limit", lk: "itemPeakLimit", icon: "mdi:flash" },
    ],
  },
  {
    tk: "secTempLimit",
    icon: "mdi:thermometer-alert",
    items: [
      { key: "temp_charge_limit", domain: "switch", lk: "itemEnable", icon: "mdi:thermometer-alert", gate: true },
      { key: "temp_charge_limit_c", lk: "itemTempLimitC", icon: "mdi:thermometer-high" },
      { key: "temp_charge_limit_band_c", lk: "itemTempLimitBand", icon: "mdi:thermometer-lines" },
      { key: "temp_charge_limit_floor_pct", lk: "itemTempLimitFloor", icon: "mdi:battery-charging-low" },
      { key: "temp_charge_limit_discharge", domain: "switch", lk: "itemTempApplyDischarge", icon: "mdi:battery-arrow-down" },
    ],
  },
];

// Control tab layout, by section `tk`. Sections absent from the live registry
// are skipped; an empty column/row is dropped (no gaps).
//  - `pair`: columns 1 & 2 rendered as a 2-col grid so each row's two cards
//    share a height (Manual≈Semanal, Predictiva≈Retardo, Horario≈Límites). A
//    `null` (or absent) partner leaves an invisible spacer to keep the pairing.
//  - `col`: an independent vertical stack (columns 3-5).
const SYS_LAYOUT = [
  {
    pair: [
      ["secPhaseProtection", "secManual"],
      ["secWeeklyFull", "secOffgridMeter"],
      ["diagPredictive", "diagChargeDelay"],
      ["secHourly", "secSysLimits"],
      ["diagPeak", "secTempLimit"],
    ],
  },
  { col: ["secSlots"] },
  { col: ["secExcluded"] },
  { col: ["secCommon", "secPd", "secNoPd"] },
];

// Flattened SYS_LAYOUT → the default left-to-right card order for the Control
// tab when the user hasn't reordered it (drag-and-drop persists their own order
// in localStorage; see _loadCtlOrder). Keeps the intended grouping as the seed.
const DEFAULT_SYS_ORDER = (() => {
  const out = [];
  for (const block of SYS_LAYOUT) {
    if (block.pair) for (const [a, b] of block.pair) { if (a) out.push(a); if (b) out.push(b); }
    else if (block.col) for (const tk of block.col) out.push(tk);
  }
  return out;
})();

// Control-tab help text, sourced verbatim from the options-flow data_description
// (strings.json / translations). Keyed by section tk or entity translation_key.
// Shown as a hover title + tap popover. English is the fallback (see _help).
const SYS_HELP = {
  en: {
    secPhaseProtection: "Master switch for three-phase current protection. When OFF, phase limits are ignored and battery phase selectors are unavailable. When ON, a battery without a phase remains outside the envelope and continues normal automatic operation.",
    three_phase_protection: "Enable or disable the three-phase current protection envelope.",
    battery_phase: "Select the physical AC phase for this battery. Choose Unassigned when it is not connected to a protected phase; it then remains outside the three-phase envelope and continues normal automatic operation.",
    secManual: "When ON, automatic control (PD, predictive charging, time slots, peak shaving…) is paused and every battery is set to 0 W (idle). Turn it OFF to resume automatic control.",
    secOffgridMeter: "Selects the configured off-grid power sensor as the source for control and derived statistics. It does not enable any battery off-grid/EPS port. A battery actively supplying its own off-grid output remains excluded from PD.",
    vacation_mode: "When ON, household-consumption learning and the legacy daily average are paused. Physical consumption meters, the daily-operation graph and battery control continue normally. Forecasts use a constant baseline calculated from 01:00–05:00: a night is valid after 3 hours of coverage, using the median of up to the last three valid nights. Turn it OFF to resume learning; vacation data remains excluded from Recorder backfill.",
    battery_manual_mode: "When ON, this battery is idled once and removed from automatic control. Its manual force mode and setpoints can then be selected while other batteries continue automatically. Omnibattery software limits do not constrain it, but the battery's own BMS/driver protections still apply. Global Manual Mode is separate.",
    fronius_internal_control_disabled: "When ON (recommended), Omnibattery retains external storage control with a 0/0 idle window during setup, reload and orderly shutdown. Turning it OFF explicitly returns control to Fronius and removes BYD from Omnibattery's automatic pool.",
    secWeeklyFull: "Select the day of the week when batteries should charge to 100% for cell balancing. After reaching 100%, the system reverts to your configured maximum charge limit.",
    secSlots: "Define when and how the batteries are allowed to operate. The ticks control each direction, SOC and power. Manual mode forces an exact power, bypassing the PD algorithm.",
    secExcluded: "Configure devices with special management: you can EXCLUDE devices that should NOT be powered by battery, or ADD devices that SHOULD be powered by battery even if they're not in the home consumption sensor.",
    secCommon: "System-wide controls. Vacation Mode pauses household-consumption learning without stopping physical metering or battery control. The remaining knobs are shared by both the PD controller and No-PD direct tracking; changing them affects whichever control mode is active.",
    secPd: "Configure advanced PD controller parameters for expert tuning of battery charge/discharge behavior. Only modify these if you understand PID control theory. Default values work well for most installations.",
    secNoPd: "When ON, the PD controller is bypassed and each battery tracks the grid setpoint 1:1 (raw, kp=1, no integral/derivative/smoothing/rate-limit). It still reuses the deadband, min charge/discharge power, relay cooldown and target-grid-power knobs above. Use only if PD tuning can't tame your meter; PD is the safer default.",
    no_pd_command_delay: "Collapse-debounce window for No-PD mode. Grid-sensor updates arriving within this window collapse into a single command issued on the latest value, so a fast meter can't flood the bus. 0 = act on every event (paced only by PD min cycle interval). Range: 0–3 s, step 0.1, default: 0 s.",
    diagPredictive: "Charges batteries from the grid during off-peak hours when today's solar forecast is insufficient.",
    smart_predischarge: "Opt-in dynamic-pricing anti-curtailment. It creates headroom before forecast PV reaches a negative-price window, while preserving SOC floors, user ownership and battery safety limits.",
    negative_price_charging: "Opt-in dynamic-pricing charging when the normalized grid import price is negative, even when the forecast has no energy deficit. Charging stops at each battery's configured maximum SOC.",
    negative_injection_threshold: "Price at or below which a future slot is protected when forecast PV exceeds estimated household consumption. The comparison is inclusive (<=).",
    predischarge_reserve_soc: "Additional SOC floor for pre-discharge. 0 uses each battery's existing minimum and guaranteed SOC floors.",
    predischarge_max_export_power_w: "Maximum deliberate grid export during pre-discharge. 0 W means self-consumption only; the planner never controls the PV inverter.",
    curtailment_status: "Live plan diagnostics: risk windows, current/required headroom, selected expensive slots, targets and any shortfall or fail-safe reason.",
    diagChargeDelay: "Delays battery charging until the solar energy balance indicates it's needed, exporting excess solar to grid in the meantime.",
    secHourly: "Tracks grid import/export per hour and automatically adjusts the battery setpoint to achieve a target net energy balance.\n\n⚠️ Only useful in Spain, under the hourly surplus-compensation scheme (RD 244/2019), where grid surplus is settled hour by hour. In feed-in-tariff or annual-net-metering markets it provides no benefit and may cause lost export revenue and unnecessary battery cycling.",
    diagPeak: "When enabled, if battery SOC drops below a threshold, the system conserves energy by only discharging to offset consumption above a peak limit.",
    secSysLimits: "When enabled, the two sliders below cap the combined charge/discharge power of all active batteries.",
    excluded_device_enabled: "✓ CHECKED = Home sensor ALREADY includes this device → Battery will NOT power it (excluded). ✗ UNCHECKED = Home sensor doesn't see it → Battery WILL power it (additional)",
    excluded_device_solar_surplus: "If checked, the device will be able to consume energy directly from solar panels (surplus) without the battery trying to compensate. Recommended for high consumption devices like EV chargers.",
    excluded_device_dynamic_power_control: "For devices that dynamically adjust their own demand using a grid meter. Requires Solar Surplus and an activity / EV charging sensor, which requests priority before power appears. Genuine leftover export may still charge the battery.",
    excluded_device_cover_home: "If ON (needs Solar Surplus + a solar sensor), the battery covers the home's own load while this device runs, importing from grid only for the device itself. If OFF, the battery stays idle whenever the device is active.",
    excluded_device_exclusion_pct: "How much of this device's demand stays excluded from the battery. 100% (default) = fully excluded (battery never powers it); lower values let the battery cover the rest (e.g. 60% → battery may cover 40%). Only affects devices with a power sensor.",
    weekly_full_charge_enabled: "When ON, batteries charge to 100% one day per week (chosen below) for cell balancing, then revert to your configured max SOC.",
    dp_price_discharge_control: "When ON, the battery only discharges when the current price is above the max price threshold (or today's auto average if unset). If time slots restrict discharge, both conditions must be met.",
    reevaluate_dynamic_pricing: "Rebuild today's dynamic-pricing charge schedule right now, using the latest prices and solar forecast, instead of waiting for the automatic daily run.",
    rt_price_discharge_control: "When ON, the battery only discharges when the current price is above the threshold (fixed or daily average). If time slots restrict discharge, both conditions must be met.",
    hourly_balance_target_net_wh: "Target net grid energy per hour. 0 = neutral (no net import/export). Positive = aim to import this much; negative = aim to export. Range -2 to 2 kWh.",
    hourly_balance_max_offset_w: "Maximum power adjustment the hourly balancer may apply to the battery setpoint. Higher = corrects faster but more aggressively. Range 100–5000 W.",
    hourly_balance_deadband_wh: "Net-energy deadband. If the hour's deviation from target stays within this band, no correction is applied. Range 0–0.5 kWh.",
    hourly_balance_hysteresis_w: "Minimum change in the computed offset before the setpoint is updated, to avoid jitter. Range 0–200 W.",
    weekly_full_charge_day: "Day when batteries will charge to 100% regardless of configured max SOC. This helps balance battery cells.",
    pd_tuning_profile: "One-click PD presets, smoothest → fastest. Sets Kp, Kd and max power change together (deadband stays separate). Moving any of those sliders switches to Custom. Smoother = calmer but slower; more aggressive = faster but can overshoot.",
    system_pd_control_quality: "How well the PD holds the grid target. Stable = good; Oscillating = hunting (try a smoother profile or a wider deadband); Sluggish = too slow (try a more aggressive profile); Battery limited = battery full/empty, not a tuning problem. Allow 1-2 min after a change.",
    pd_controller_kp: "Responsiveness to grid imbalance. Higher values = faster response but risk of overshoot. Range: 0.1-2.0, default: 0.35",
    pd_controller_kd: "Damping to prevent oscillation. Higher values = smoother transitions but slower settling. Range: 0.0-2.0, default: 0.3",
    pd_controller_deadband: "Grid power tolerance around zero. Prevents micro-adjustments to minor fluctuations. Higher values reduce sensitivity. Range: 0-200W, default: 40W",
    pd_controller_max_power_change: "Maximum battery power change per control cycle (2.5s). Prevents abrupt commands. Lower values = smoother but slower. Range: 100-2000W, default: 800W",
    pd_controller_direction_hysteresis: "Power threshold required to switch between charging and discharging. Prevents rapid direction changes. Range: 0-200W, default: 60W",
    pd_min_charge_power: "Minimum power for charging. Below this threshold, the controller stays idle instead of charging at low power. 0 = disabled.",
    pd_min_discharge_power: "Minimum power for discharging. Below this threshold, the controller stays idle instead of discharging at low power. 0 = disabled.",
    pd_relay_cooldown: "Anti-chatter: once the battery engages, it stays on at least this long before returning to idle, so the relay doesn't toggle when the grid hovers at the deadband edge during solar ramp-up/down. While held it runs at the PD min charge/discharge power (or 100 W if that is 0). Large imbalances bypass it. 0 = disabled.",
    pd_min_cycle_interval: "Minimum spacing between event-driven control cycles. Grid-sensor updates arriving sooner than this are dropped, so a fast meter can't flood slow Modbus bridges (e.g. Elfin EW11) with write bursts. The 2 s safety timer is never gated. 0 = disabled.",
    pd_target_grid_power: "Grid power setpoint the controller regulates to. Positive = import from grid (battery charges), negative = export to grid (battery discharges), 0 = net zero. The range follows your total configured battery power, narrowed by the system power limits when enabled. Default: 0 W.",
    system_max_charge_power: "Optional cap for combined charge power across all active batteries. 0 = disabled; per-battery limits still apply.",
    system_max_discharge_power: "Optional cap for combined discharge power across all active batteries. 0 = disabled; per-battery limits still apply.",
    max_contracted_power: "Total contracted power (ICP) in watts. System won't exceed this limit when charging to avoid tripping the breaker",
    predictive_safety_margin_kwh: "Extra solar-forecast buffer used both when deciding whether to charge and when preparing headroom for anti-curtailment. Set to 0 to disable (default). Capped at total battery capacity.",
    predictive_grid_charge_margin_pct: "Extra % charged from the grid on top of the solar-deficit, to hedge against optimistic solar forecasts or worse-than-expected weather. Example: a 2 kWh grid need at 50 % charges 3 kWh. Set to 0 to disable (default). Capped at the gap to max SOC.",
    min_soc_floor_enabled: "Master switch for the guaranteed minimum SOC. When on, the overnight grid charge honours the SOC floor set below; when off, the floor is ignored and charging follows the solar forecast alone.",
    predictive_min_soc_floor: "Forces an overnight grid charge to reach at least this SOC by the end of the charging window, even when the whole-day solar forecast shows no deficit. Covers the morning gap before solar ramps up. Set to 0 to disable (default).",
    delay_safety_margin_min: "Hours before sunset by which charging must be complete. Higher values unlock charging earlier.",
    charge_delay_balance_deadband_kwh: "Tolerance on the energy-balance check. The delay only unlocks when usable battery + solar forecast falls short of expected consumption by more than this. Higher values hold the delay longer on balanced days; 0 = unlock on any shortfall.",
    delay_soc_setpoint_enabled: "When on, the battery first charges to the delay target SOC before the solar charge delay holds off further charging.",
    delay_soc_setpoint: "The SOC the battery must reach before the solar delay kicks in. Minimum is 12 % — the Venus battery minimum discharge SOC.",
    capacity_protection_soc_threshold: "When average battery SOC drops below this value, capacity protection activates. The battery will stop discharging for normal consumption and only cover peaks above the limit.",
    capacity_protection_limit: "Grid import power threshold. When house consumption exceeds this value and protection is active, the battery discharges only the excess above this limit.",
    capacity_protection_excluded_devices: "When enabled, the battery also covers the portion of excluded-device demand that would push grid import above the peak limit. Normal home coverage and battery safety limits remain unchanged.",
    secTempLimit: "When enabled, charge power is reduced when a battery gets hot: full power at or below the temperature limit, ramping down to the minimum over the band, and back up as it cools.",
    temp_charge_limit_c: "Charging runs at full power at or below this temperature; above it the derate begins.",
    temp_charge_limit_band_c: "Temperature range above the limit over which charge power ramps down to the minimum.",
    temp_charge_limit_floor_pct: "Charge power at the limit plus the band, as a percentage of the normal charge ceiling. 0% stops charging entirely when very hot.",
    temp_charge_limit_discharge: "Apply the same temperature derate to discharge power. Discharge tolerates heat better, so this shares the charge threshold as a compromise; mainly it keeps discharge under the BMS hard cutoff.",
    max_price_threshold: "Charge ceiling for dynamic pricing: the battery only grid-charges when the price is at or below this. Leave empty to fall back to the daily-average price. Must stay ≤ the discharge floor.",
    discharge_price_threshold: "Discharge floor for dynamic pricing: the battery only discharges when the price is at or above this. Leave empty to fall back (charge ceiling, else daily average). Must stay ≥ the charge ceiling.",
    min_arbitrage_margin: "Minimum profit per kWh required before grid charging. 0 or empty = off, and the charge ceiling alone decides. When set, the ceiling follows the day's spread: charging is skipped when the expensive hours are not far enough above the cheap ones to repay conversion losses.",
    round_trip_efficiency: "Battery round-trip efficiency (kWh out / kWh in) used to value stored energy for the arbitrage margin. Lower values make the gate stricter. Only used when a minimum arbitrage margin is set.",
  },
  es: {
    secPhaseProtection: "Interruptor general de la protección de corriente trifásica. Al desactivarlo se ignoran los límites de fase y los selectores de fase de las baterías no están disponibles. Al activarlo, una batería sin fase queda fuera de la envolvente y sigue funcionando normalmente en automático.",
    three_phase_protection: "Activa o desactiva la envolvente de protección de corriente trifásica.",
    battery_phase: "Selecciona la fase física de CA de esta batería. Elige Sin asignar si no está conectada a una fase protegida; quedará fuera de la envolvente trifásica y seguirá funcionando normalmente en automático.",
    secManual: "Cuando está ACTIVADO, el control automático (PD, carga predictiva, franjas horarias, reducción de picos…) se pausa y todas las baterías se ponen a 0 W (en reposo). DESACTÍVALO para reanudar el control automático.",
    secOffgridMeter: "Selecciona el sensor de potencia off-grid configurado como fuente del control y de las estadísticas derivadas. No habilita ningún puerto off-grid/EPS. Una batería que suministre por su propia salida off-grid sigue excluida del PD.",
    vacation_mode: "Al ACTIVARLO se pausan el aprendizaje del consumo doméstico y la media diaria heredada. Los contadores físicos, el gráfico de operación diaria y el control de las baterías siguen funcionando normalmente. Las previsiones usan un baseline constante calculado entre las 01:00 y las 05:00: una noche es válida con 3 horas de cobertura y se usa la mediana de hasta las tres últimas noches válidas. DESACTÍVALO para reanudar el aprendizaje; los datos vacacionales seguirán excluidos del backfill de Recorder.",
    battery_manual_mode: "Al ACTIVARLO, esta batería pasa una vez a 0 W y sale del control automático. Sus modos y consignas manuales se pueden elegir entonces; las demás baterías continúan en automático. Los límites de software de Omnibattery no la restringen, pero sí las protecciones propias del BMS/driver. El Modo manual global es independiente.",
    fronius_internal_control_disabled: "ACTIVADO (recomendado) mantiene el control externo con una ventana de reposo 0/0 durante configuración, recarga y apagado. DESACTIVADO devuelve explícitamente el control a Fronius y elimina BYD del grupo automático.",
    secWeeklyFull: "Selecciona el día de la semana en el que las baterías deben cargarse al 100% para el balanceo de celdas. Una vez alcanzado el 100%, el sistema revertirá al límite de carga máximo configurado.",
    secSlots: "Define cuándo y cómo se permite operar a las baterías. Los ticks permiten controlar cada dirección, el SOC y la potencia. El modo manual fuerza una potencia exacta ignorando el algoritmo PD.",
    secExcluded: "Configura dispositivos con gestión especial: puedes EXCLUIR dispositivos que NO deben alimentarse por batería, o AÑADIR dispositivos que SÍ debe alimentar la batería aunque no estén en el sensor de consumo del hogar.",
    secCommon: "Controles generales del sistema. Modo vacaciones pausa el aprendizaje del consumo doméstico sin detener la medición física ni el control de las baterías. El resto de parámetros se comparte entre el controlador PD y el seguimiento directo sin PD; cambiarlos afecta al modo que esté activo.",
    secPd: "Configura parámetros avanzados del controlador PD para ajustar el comportamiento de carga/descarga de las baterías. Solo modifica estos valores si comprendes la teoría de control PID. Los valores predeterminados funcionan bien para la mayoría de instalaciones.",
    secNoPd: "Cuando está ACTIVADO, se omite el controlador PD y cada batería sigue la consigna de red 1:1 (en bruto, kp=1, sin integral/derivativo/suavizado/límite de variación). Sigue reutilizando la banda muerta, las potencias mín. de carga/descarga, el tiempo de relé y la potencia objetivo de red de arriba. Úsalo solo si el ajuste PD no puede domar tu medidor; PD es el valor por defecto más seguro.",
    no_pd_command_delay: "Ventana de agrupación (debounce) para el modo sin PD. Las actualizaciones del sensor de red que llegan dentro de esta ventana se agrupan en una sola orden emitida con el último valor, para que un medidor rápido no sature el bus. 0 = actuar en cada evento (acotado solo por el intervalo mín. de ciclo PD). Rango: 0–3 s, paso 0,1, por defecto: 0 s.",
    diagPredictive: "Carga las baterías desde red durante horas valle cuando la predicción solar del día de hoy es insuficiente.",
    diagChargeDelay: "Retrasa la carga de las baterías hasta que el balance energético solar indique que es necesario, exportando el excedente a red mientras tanto.",
    secHourly: "Registra la importación/exportación de red por hora y ajusta automáticamente el setpoint de la batería para alcanzar un balance de energía objetivo.\n\n⚠️ Solo tiene utilidad en España, bajo el esquema de compensación de excedentes horaria (RD 244/2019), donde el excedente vertido a la red se liquida hora a hora. En mercados con tarifa de inyección (feed-in) o balance neto anual no aporta beneficio y puede causar pérdida de ingresos por exportación y ciclado innecesario de la batería.",
    diagPeak: "Si se activa, cuando el SOC de la batería baje de un umbral, el sistema conservará energía descargando solo para cubrir consumo que supere un límite pico.",
    secSysLimits: "Al activarlo, los dos sliders inferiores limitan la potencia combinada de carga/descarga de todas las baterias activas.",
    excluded_device_enabled: "✓ MARCADO = El sensor de consumo del hogar YA incluye este dispositivo → La batería NO lo alimentará (excluido). ✗ DESMARCADO = El sensor del hogar NO lo ve → La batería SÍ lo alimentará (adicional)",
    excluded_device_solar_surplus: "Si se marca, el dispositivo podrá consumir energía directamente de los paneles solares (excedente) sin que la batería intente compensarlo. Se recomienda marcar para dispositivos de gran consumo como cargadores de VE.",
    excluded_device_dynamic_power_control: "Para dispositivos que ajustan dinámicamente su demanda mediante un contador de red. Requiere Excedente Solar y un sensor de actividad / carga del VE, que pide prioridad antes de que aparezca potencia. El excedente residual real todavía puede cargar la batería.",
    excluded_device_cover_home: "Si se activa (requiere Excedente Solar + sensor solar), la batería cubre el consumo propio del hogar mientras el dispositivo funciona, importando de red solo para el dispositivo. Si se desactiva, la batería permanece inactiva mientras el dispositivo esté activo.",
    excluded_device_exclusion_pct: "Qué parte de la demanda de este aparato se mantiene excluida de la batería. 100% (por defecto) = totalmente excluido (la batería nunca lo alimenta); valores menores dejan que la batería cubra el resto (ej. 60% → la batería puede cubrir el 40%). Solo afecta a aparatos con sensor de potencia.",
    weekly_full_charge_enabled: "Si está activado, las baterías se cargan al 100% un día a la semana (elegido abajo) para equilibrar las celdas; después vuelven al SOC máximo configurado.",
    dp_price_discharge_control: "Si está activado, la batería solo descarga cuando el precio actual supera el umbral máximo (o la media diaria automática si no se configura). Si las franjas horarias restringen la descarga, deben cumplirse ambas condiciones.",
    reevaluate_dynamic_pricing: "Recalcula ahora mismo la planificación de carga por precios dinámicos de hoy, usando los precios y la previsión solar más recientes, sin esperar a la ejecución diaria automática.",
    negative_price_charging: "Carga opcional con precios dinámicos cuando el precio normalizado de importación es negativo, aunque no exista déficit previsto. Se detiene en el SOC máximo configurado de cada batería.",
    rt_price_discharge_control: "Si está activado, la batería solo descarga cuando el precio actual supera el umbral (fijo o media diaria). Si las franjas horarias restringen la descarga, deben cumplirse ambas condiciones.",
    hourly_balance_target_net_wh: "Energía neta de red objetivo por hora. 0 = neutro (sin importación/exportación neta). Positivo = importar esa cantidad; negativo = exportar. Rango -2 a 2 kWh.",
    hourly_balance_max_offset_w: "Ajuste máximo de potencia que el balance horario puede aplicar al setpoint de la batería. Mayor = corrige más rápido pero más agresivo. Rango 100–5000 W.",
    hourly_balance_deadband_wh: "Banda muerta de energía neta. Si la desviación de la hora respecto al objetivo se mantiene dentro de esta banda, no se aplica corrección. Rango 0–0,5 kWh.",
    hourly_balance_hysteresis_w: "Cambio mínimo en el offset calculado antes de actualizar el setpoint, para evitar oscilaciones. Rango 0–200 W.",
    weekly_full_charge_day: "Día en el que las baterías se cargarán al 100% independientemente del SOC máximo configurado. Esto ayuda a equilibrar las celdas de la batería.",
    pd_tuning_profile: "Presets de PD en un clic, de más suave a más rápido. Ajusta Kp, Kd y el cambio máx. de potencia a la vez (el deadband va aparte). Mover cualquiera de esos sliders pasa a Personalizado. Más suave = más calmado pero lento; más agresivo = más rápido pero puede sobreoscilar.",
    system_pd_control_quality: "Cómo de bien mantiene el PD el objetivo de red. Estable = bien; Oscilando = cabeceo (usa un perfil más suave o sube el deadband); Lento = demasiado lento (usa un perfil más agresivo); Limitado por batería = batería llena/vacía, no es problema de ajuste. Espera 1-2 min tras un cambio.",
    pd_controller_kp: "Capacidad de respuesta al desequilibrio de red. Valores más altos = respuesta más rápida pero riesgo de sobreoscilación. Rango: 0.1-2.0, predeterminado: 0.35",
    pd_controller_kd: "Amortiguación para prevenir oscilaciones. Valores más altos = transiciones más suaves pero asentamiento más lento. Rango: 0.0-2.0, predeterminado: 0.3",
    pd_controller_deadband: "Tolerancia de potencia de red alrededor de cero. Previene microajustes ante fluctuaciones menores. Valores más altos reducen la sensibilidad. Rango: 0-200W, predeterminado: 40W",
    pd_controller_max_power_change: "Cambio máximo de potencia de batería por ciclo de control (2.5s). Previene comandos abruptos. Valores más bajos = más suave pero más lento. Rango: 100-2000W, predeterminado: 800W",
    pd_controller_direction_hysteresis: "Umbral de potencia requerido para cambiar entre carga y descarga. Previene cambios rápidos de dirección. Rango: 0-200W, predeterminado: 60W",
    pd_min_charge_power: "Potencia mínima para cargar. Por debajo de este umbral, el controlador queda en reposo en vez de cargar a baja potencia. 0 = desactivado.",
    pd_min_discharge_power: "Potencia mínima para descargar. Por debajo de este umbral, el controlador queda en reposo en vez de descargar a baja potencia. 0 = desactivado.",
    pd_relay_cooldown: "Anti-chasquido: una vez la batería engancha, sigue activa al menos este tiempo antes de volver a reposo, para que el relé no conmute cuando la red ronda el borde de la banda muerta durante la rampa solar (amanecer/anochecer). Mientras se mantiene, funciona a la potencia mín. de carga/descarga PD (o 100 W si es 0). Desequilibrios grandes lo saltan. 0 = desactivado.",
    pd_min_cycle_interval: "Separación mínima entre ciclos de control disparados por evento. Las actualizaciones del sensor de red que llegan antes de este tiempo se descartan, para que un medidor rápido no sature puentes Modbus lentos (p. ej. Elfin EW11) con ráfagas de escritura. El temporizador de seguridad de 2 s nunca se frena. 0 = desactivado.",
    pd_target_grid_power: "Consigna de potencia de red que regula el controlador. Positivo = importar de red (la batería carga), negativo = exportar a red (la batería descarga), 0 = balance neto cero. El rango sigue la potencia total configurada de tus baterías, limitado por los límites de potencia del sistema cuando están activos. Valor por defecto: 0 W.",
    system_max_charge_power: "Limite opcional para la potencia de carga combinada de todas las baterias activas. 0 = desactivado; los limites por bateria siguen aplicandose.",
    system_max_discharge_power: "Limite opcional para la potencia de descarga combinada de todas las baterias activas. 0 = desactivado; los limites por bateria siguen aplicandose.",
    max_contracted_power: "Potencia total contratada (ICP) en vatios. El sistema no superará este límite al cargar para evitar que salte el diferencial",
    predictive_safety_margin_kwh: "Margen adicional de la previsión solar usado tanto para decidir si cargar como para preparar espacio frente al anti-vertido. Pon 0 para desactivar (por defecto). Limitado a la capacidad total de la batería.",
    predictive_grid_charge_margin_pct: "Porcentaje extra cargado desde la red sobre el déficit solar, para cubrir previsiones solares optimistas o peor tiempo del esperado. Ejemplo: una necesidad de 2 kWh de red al 50 % carga 3 kWh. Pon 0 para desactivar (por defecto). Limitado al margen hasta el SOC máximo.",
    min_soc_floor_enabled: "Interruptor principal del SOC mínimo garantizado. Si está activado, la carga de red nocturna respeta el suelo de SOC fijado abajo; si está desactivado, se ignora el suelo y la carga sigue solo la previsión solar.",
    predictive_min_soc_floor: "Fuerza una carga de red nocturna para alcanzar al menos este SOC al final de la ventana de carga, aunque la previsión solar del día no muestre déficit. Cubre el hueco matinal antes de que arranque el solar. Pon 0 para desactivar (por defecto).",
    delay_safety_margin_min: "Horas antes de la puesta de sol en las que se garantiza que la carga habrá terminado. Valores más altos desbloquean la carga antes.",
    charge_delay_balance_deadband_kwh: "Tolerancia en el cálculo de balance energético. El retardo solo se desbloquea cuando batería utilizable + previsión solar queda por debajo del consumo esperado en más de este valor. Valores más altos mantienen el retardo más tiempo en días equilibrados; 0 = desbloquear ante cualquier déficit.",
    delay_soc_setpoint_enabled: "Al activarlo, la batería carga primero hasta el SOC objetivo antes de que el retraso de carga solar detenga la carga.",
    delay_soc_setpoint: "SOC mínimo que debe alcanzar la batería antes de que el retraso solar entre en funcionamiento. El valor mínimo es el 12 % (SOC mínimo de descarga de las baterías Venus).",
    capacity_protection_soc_threshold: "Cuando el SOC medio de las baterías baje de este valor, se activa la reducción de picos. La batería dejará de descargar para consumo normal y solo cubrirá picos por encima del límite.",
    capacity_protection_limit: "Umbral de potencia de importación de red. Cuando el consumo de la casa supere este valor y la reducción de picos esté activa, la batería solo descargará el exceso por encima de este límite.",
    capacity_protection_excluded_devices: "Si se activa, la batería también cubre la parte de la demanda excluida que haría superar el límite de pico de la red. La cobertura normal del hogar y las protecciones de la batería no cambian.",
    secTempLimit: "Cuando está activado, la potencia de carga se reduce cuando una batería se calienta: plena potencia al límite de temperatura o por debajo, bajando hasta el mínimo a lo largo de la banda y subiendo de nuevo al enfriarse.",
    temp_charge_limit_c: "La carga funciona a plena potencia a esta temperatura o por debajo; por encima empieza la reducción.",
    temp_charge_limit_band_c: "Rango de temperatura por encima del límite a lo largo del cual la potencia de carga baja hasta el mínimo.",
    temp_charge_limit_floor_pct: "Potencia de carga en el límite más la banda, como porcentaje del techo de carga normal. 0 % detiene la carga por completo cuando hace mucho calor.",
    temp_charge_limit_discharge: "Aplica la misma reducción por temperatura a la potencia de descarga. La descarga tolera mejor el calor, así que comparte el umbral de carga como compromiso; sobre todo mantiene la descarga por debajo del corte duro del BMS.",
    max_price_threshold: "Techo de carga para precios dinámicos: la batería solo carga de red cuando el precio está en o por debajo de este valor. Vacío = precio medio diario. Debe mantenerse ≤ el suelo de descarga.",
    discharge_price_threshold: "Suelo de descarga para precios dinámicos: la batería solo descarga cuando el precio está en o por encima de este valor. Vacío = techo de carga o precio medio diario. Debe mantenerse ≥ el techo de carga.",
    min_arbitrage_margin: "Beneficio mínimo por kWh exigido antes de cargar de red. 0 o vacío = desactivado. Si se define, el techo sigue el diferencial del día: no se carga cuando las horas caras no superan lo suficiente a las baratas para compensar las pérdidas de conversión.",
    round_trip_efficiency: "Eficiencia de ciclo completo (kWh de salida / kWh de entrada) usada para valorar la energía almacenada. Valores más bajos endurecen el filtro. Solo se usa si hay un margen mínimo de arbitraje.",
  },
  ca: {
    secManual: "Quan està ACTIVAT, el control automàtic (PD, càrrega predictiva, franges horàries, reducció de pics…) es pausa i totes les bateries es posen a 0 W (en repòs). DESACTIVA'L per reprendre el control automàtic.",
    vacation_mode: "Quan està ACTIVAT, es pausen l'aprenentatge del consum domèstic i la mitjana diària heretada. Els comptadors físics, el gràfic d'operació diària i el control de les bateries continuen funcionant normalment. Les previsions utilitzen un baseline constant calculat entre la 01:00 i les 05:00: una nit és vàlida amb 3 hores de cobertura i s'utilitza la mediana de fins a les tres últimes nits vàlides. DESACTIVA'L per reprendre l'aprenentatge; les dades de vacances continuaran excloses del backfill de Recorder.",
    battery_manual_mode: "Quan s'ACTIVA, aquesta bateria passa una vegada a 0 W i surt del control automàtic. Els seus modes i consignes manuals es poden triar aleshores; les altres bateries segueixen en automàtic. Els límits de programari d'Omnibattery no la restringeixen, però sí les proteccions pròpies del BMS/driver. El mode manual global és independent.",
    fronius_internal_control_disabled: "ACTIVAT (recomanat) manté el control extern amb una finestra de repòs 0/0 durant configuració, recàrrega i aturada. DESACTIVAT retorna explícitament el control a Fronius i treu BYD del grup automàtic.",
    secWeeklyFull: "Selecciona el dia de la setmana en què les bateries s'han de carregar al 100% per a l'equilibratge de cel·les. Un cop assolit el 100%, el sistema tornarà al límit de càrrega màxim configurat.",
    secSlots: "Defineix quan i com es permet operar a les bateries. Els ticks permeten controlar cada direcció, el SOC i la potència. El mode manual força una potència exacta ignorant l'algorisme PD.",
    secExcluded: "Configura dispositius amb gestió especial: pots EXCLOURE dispositius que NO s'han d'alimentar per bateria, o AFEGIR dispositius que SÍ ha d'alimentar la bateria encara que no estiguin al sensor de consum de la llar.",
    secCommon: "Controls generals del sistema. El Mode vacances pausa l'aprenentatge del consum domèstic sense aturar la mesura física ni el control de les bateries. La resta de paràmetres es comparteixen entre el controlador PD i el seguiment directe sense PD; canviar-los afecta el mode actiu.",
    secPd: "Configura paràmetres avançats del controlador PD per ajustar el comportament de càrrega/descàrrega de les bateries. Només modifica aquests valors si comprens la teoria de control PID. Els valors per defecte funcionen bé per a la majoria d'instal·lacions.",
    secNoPd: "Quan està ACTIVAT, s'omet el controlador PD i cada bateria segueix la consigna de xarxa 1:1 (en brut, kp=1, sense integral/derivatiu/suavitzat/límit de variació). Continua reutilitzant la banda morta, les potències mín. de càrrega/descàrrega, el temps de relé i la potència objectiu de xarxa de dalt. Usa'l només si l'ajust PD no pot domar el teu mesurador; PD és el valor per defecte més segur.",
    no_pd_command_delay: "Finestra d'agrupació (debounce) per al mode sense PD. Les actualitzacions del sensor de xarxa que arriben dins d'aquesta finestra s'agrupen en una sola ordre emesa amb l'últim valor, perquè un mesurador ràpid no saturi el bus. 0 = actuar en cada esdeveniment (acotat només per l'interval mín. de cicle PD). Rang: 0–3 s, pas 0,1, per defecte: 0 s.",
    diagPredictive: "Carrega les bateries des de la xarxa durant hores vall quan la previsió solar d'avui és insuficient.",
    diagChargeDelay: "Retarda la càrrega de les bateries fins que el balanç energètic solar indiqui que cal, exportant l'excedent a la xarxa mentrestant.",
    secHourly: "Registra la importació/exportació de xarxa per hora i ajusta automàticament el setpoint de la bateria per assolir un balanç d'energia objectiu.\n\n⚠️ Només té utilitat a Espanya, sota l'esquema de compensació d'excedents horària (RD 244/2019), on l'excedent abocat a la xarxa es liquida hora a hora. En mercats amb tarifa d'injecció (feed-in) o balanç net anual no aporta cap benefici i pot causar pèrdua d'ingressos per exportació i cicles innecessaris de la bateria.",
    diagPeak: "Si s'activa, quan el SOC de la bateria baixi d'un llindar, el sistema conservarà energia descarregant només per cobrir consum que superi un límit de pic.",
    secSysLimits: "En activar-lo, els dos sliders inferiors limiten la potència combinada de càrrega/descàrrega de totes les bateries actives.",
    excluded_device_enabled: "✓ MARCAT = El sensor de consum de la llar JA inclou aquest dispositiu → La bateria NO l'alimentarà (exclòs). ✗ DESMARCAT = El sensor de la llar NO el veu → La bateria SÍ l'alimentarà (addicional)",
    excluded_device_solar_surplus: "Si es marca, el dispositiu podrà consumir energia directament dels panells solars (excedent) sense que la bateria intenti compensar-ho. Es recomana marcar per a dispositius de gran consum com carregadors de VE.",
    excluded_device_dynamic_power_control: "Per a dispositius que ajusten dinàmicament la demanda mitjançant un comptador de xarxa. Requereix Excedent Solar i un sensor d'activitat / càrrega del VE, que demana prioritat abans que aparegui potència. L'excedent residual real encara pot carregar la bateria.",
    excluded_device_cover_home: "Si s'activa (requereix Excedent Solar + sensor solar), la bateria cobreix el consum propi de la llar mentre el dispositiu funciona, important de xarxa només per al dispositiu. Si es desactiva, la bateria roman inactiva mentre el dispositiu estigui actiu.",
    weekly_full_charge_enabled: "Si està activat, les bateries es carreguen al 100% un dia a la setmana (triat a sota) per equilibrar les cel·les; després tornen al SOC màxim configurat.",
    dp_price_discharge_control: "Si està activat, la bateria només descarrega quan el preu actual supera el llindar màxim (o la mitjana diària automàtica si no es configura). Si les franges horàries restringeixen la descàrrega, s'han de complir totes dues condicions.",
    reevaluate_dynamic_pricing: "Recalcula ara mateix la planificació de càrrega per preus dinàmics d'avui, amb els preus i la previsió solar més recents, sense esperar l'execució diària automàtica.",
    rt_price_discharge_control: "Si està activat, la bateria només descarrega quan el preu actual supera el llindar (fix o mitjana diària). Si les franges horàries restringeixen la descàrrega, s'han de complir totes dues condicions.",
    hourly_balance_target_net_wh: "Energia neta de xarxa objectiu per hora. 0 = neutre (sense importació/exportació neta). Positiu = importar aquesta quantitat; negatiu = exportar. Rang -2 a 2 kWh.",
    hourly_balance_max_offset_w: "Ajust màxim de potència que el balanç horari pot aplicar al setpoint de la bateria. Major = corregeix més ràpid però més agressiu. Rang 100–5000 W.",
    hourly_balance_deadband_wh: "Banda morta d'energia neta. Si la desviació de l'hora respecte a l'objectiu es manté dins d'aquesta banda, no s'aplica correcció. Rang 0–0,5 kWh.",
    hourly_balance_hysteresis_w: "Canvi mínim en l'offset calculat abans d'actualitzar el setpoint, per evitar oscil·lacions. Rang 0–200 W.",
    weekly_full_charge_day: "Dia en què les bateries es carregaran al 100% independentment del SOC màxim configurat. Això ajuda a equilibrar les cel·les de la bateria.",
    pd_tuning_profile: "Presets de PD en un clic, de més suau a més ràpid. Ajusta Kp, Kd i el canvi màx. de potència alhora (el deadband va a part). Moure qualsevol d'aquests sliders passa a Personalitzat. Més suau = més calmat però lent; més agressiu = més ràpid però pot sobreoscil·lar.",
    system_pd_control_quality: "Com de bé manté el PD l'objectiu de xarxa. Estable = bé; Oscil·lant = cabeceig (fes servir un perfil més suau o apuja el deadband); Lent = massa lent (fes servir un perfil més agressiu); Limitat per bateria = bateria plena/buida, no és problema d'ajust. Espera 1-2 min després d'un canvi.",
    pd_controller_kp: "Capacitat de resposta al desequilibri de xarxa. Valors més alts = resposta més ràpida però risc de sobreoscil·lació. Rang: 0.1-2.0, per defecte: 0.35",
    pd_controller_kd: "Esmorteïment per prevenir oscil·lacions. Valors més alts = transicions més suaus però assentament més lent. Rang: 0.0-2.0, per defecte: 0.3",
    pd_controller_deadband: "Tolerància de potència de xarxa al voltant de zero. Evita microajustos davant fluctuacions menors. Valors més alts redueixen la sensibilitat. Rang: 0-200W, per defecte: 40W",
    pd_controller_max_power_change: "Canvi màxim de potència de bateria per cicle de control (2.5s). Evita comandes brusques. Valors més baixos = més suau però més lent. Rang: 100-2000W, per defecte: 800W",
    pd_controller_direction_hysteresis: "Llindar de potència requerit per canviar entre càrrega i descàrrega. Evita canvis ràpids de direcció. Rang: 0-200W, per defecte: 60W",
    pd_min_charge_power: "Potència mínima per carregar. Per sota d'aquest llindar, el controlador queda en repòs en lloc de carregar a baixa potència. 0 = desactivat.",
    pd_min_discharge_power: "Potència mínima per descarregar. Per sota d'aquest llindar, el controlador queda en repòs en lloc de descarregar a baixa potència. 0 = desactivat.",
    pd_relay_cooldown: "Anti-espetec: un cop la bateria s'enganxa, segueix activa almenys aquest temps abans de tornar al repòs, perquè el relé no commuti quan la xarxa ronda la vora de la banda morta durant la rampa solar (alba/capvespre). Mentre es manté, funciona a la potència mín. de càrrega/descàrrega PD (o 100 W si és 0). Desequilibris grans l'ometen. 0 = desactivat.",
    pd_min_cycle_interval: "Separació mínima entre cicles de control disparats per esdeveniment. Les actualitzacions del sensor de xarxa que arriben abans d'aquest temps es descarten, perquè un mesurador ràpid no saturi ponts Modbus lents (p. ex. Elfin EW11) amb ràfegues d'escriptura. El temporitzador de seguretat de 2 s mai es bloqueja. 0 = desactivat.",
    pd_target_grid_power: "Consigna de potència de xarxa que regula el controlador. Positiu = importar de la xarxa (la bateria carrega), negatiu = exportar a la xarxa (la bateria descarrega), 0 = balanç net zero. El rang segueix la potència total configurada de les teves bateries, limitat pels límits de potència del sistema quan estan actius. Per defecte: 0 W.",
    system_max_charge_power: "Límit opcional per a la potència de càrrega combinada de totes les bateries actives. 0 = desactivat; els límits per bateria segueixen aplicant-se.",
    system_max_discharge_power: "Límit opcional per a la potència de descàrrega combinada de totes les bateries actives. 0 = desactivat; els límits per bateria segueixen aplicant-se.",
    max_contracted_power: "Potència total contractada (ICP) en watts. El sistema no superarà aquest límit en carregar per evitar que salti el diferencial.",
    predictive_safety_margin_kwh: "Marge addicional de la previsió solar usat tant per decidir si carregar com per preparar espai davant l'anti-abocament. Posa 0 per desactivar (per defecte). Limitat a la capacitat total de la bateria.",
    predictive_grid_charge_margin_pct: "Percentatge extra carregat des de la xarxa sobre el dèficit solar, per cobrir previsions solars optimistes o pitjor temps del previst. Exemple: una necessitat de 2 kWh de xarxa al 50 % carrega 3 kWh. Posa 0 per desactivar (per defecte). Limitat al marge fins al SOC màxim.",
    min_soc_floor_enabled: "Interruptor principal del SOC mínim garantit. Si està activat, la càrrega de xarxa nocturna respecta el sòl de SOC fixat a sota; si està desactivat, s'ignora el sòl i la càrrega segueix només la previsió solar.",
    predictive_min_soc_floor: "Força una càrrega de xarxa nocturna per arribar com a mínim a aquest SOC al final de la finestra de càrrega, encara que la previsió solar del dia no mostri dèficit. Cobreix el buit del matí abans que arrenqui el solar. Posa 0 per desactivar (per defecte).",
    delay_safety_margin_min: "Hores abans de la posta de sol en què es garanteix que la càrrega haurà acabat. Valors més alts desbloquegen la càrrega abans.",
    charge_delay_balance_deadband_kwh: "Tolerància en el càlcul de balanç energètic. El retard només es desbloqueja quan bateria utilitzable + previsió solar queda per sota del consum esperat en més d'aquest valor. Valors més alts mantenen el retard més temps en dies equilibrats; 0 = desbloquejar davant de qualsevol dèficit.",
    delay_soc_setpoint_enabled: "En activar-lo, la bateria carrega primer fins al SOC objectiu abans que el retard de càrrega solar aturi la càrrega.",
    delay_soc_setpoint: "SOC mínim que ha d'assolir la bateria abans que el retard solar entri en funcionament. El valor mínim és el 12 % (SOC mínim de descàrrega de les bateries Venus).",
    capacity_protection_soc_threshold: "Quan el SOC mitjà de les bateries baixi d'aquest valor, s'activa la reducció de pics. La bateria deixarà de descarregar per a consum normal i només cobrirà pics per sobre del límit.",
    capacity_protection_limit: "Llindar de potència d'importació de xarxa. Quan el consum de la casa superi aquest valor i la reducció de pics estigui activa, la bateria només descarregarà l'excés per sobre d'aquest límit.",
    capacity_protection_excluded_devices: "Si s'activa, la bateria també cobreix la part de la demanda exclosa que faria superar el límit de pic de la xarxa. La cobertura normal de la llar i les proteccions de la bateria no canvien.",
    secTempLimit: "Quan està activat, la potència de càrrega es redueix quan una bateria s'escalfa: plena potència al límit de temperatura o per sota, baixant fins al mínim al llarg de la banda i pujant de nou en refredar-se.",
    temp_charge_limit_c: "La càrrega funciona a plena potència a aquesta temperatura o per sota; per sobre comença la reducció.",
    temp_charge_limit_band_c: "Rang de temperatura per sobre del límit al llarg del qual la potència de càrrega baixa fins al mínim.",
    temp_charge_limit_floor_pct: "Potència de càrrega al límit més la banda, com a percentatge del sostre de càrrega normal. 0 % atura la càrrega del tot quan fa molta calor.",
    temp_charge_limit_discharge: "Aplica la mateixa reducció per temperatura a la potència de descàrrega. La descàrrega tolera millor la calor, així que comparteix el llindar de càrrega com a compromís; sobretot manté la descàrrega per sota del tall dur del BMS.",
    max_price_threshold: "Sostre de càrrega per a preus dinàmics: la bateria només carrega de xarxa quan el preu està en o per sota d'aquest valor. Buit = preu mitjà diari. S'ha de mantenir ≤ el terra de descàrrega.",
    discharge_price_threshold: "Terra de descàrrega per a preus dinàmics: la bateria només descarrega quan el preu està en o per sobre d'aquest valor. Buit = sostre de càrrega o preu mitjà diari. S'ha de mantenir ≥ el sostre de càrrega.",
    min_arbitrage_margin: "Benefici mínim per kWh exigit abans de carregar de xarxa. 0 o buit = desactivat. Si es defineix, el sostre segueix el diferencial del dia: no es carrega quan les hores cares no superen prou les barates per compensar les pèrdues de conversió.",
    round_trip_efficiency: "Eficiència de cicle complet (kWh de sortida / kWh d'entrada) per valorar l'energia emmagatzemada. Valors més baixos endureixen el filtre. Només s'usa si hi ha un marge mínim d'arbitratge.",
  },
  de: {
    secManual: "Wenn EIN, wird die automatische Regelung (PD, prädiktives Laden, Zeitfenster, Lastspitzenkappung…) pausiert und jede Batterie auf 0 W (Leerlauf) gesetzt. Schalte AUS, um die automatische Regelung fortzusetzen.",
    vacation_mode: "Wenn EIN, werden das Lernen des Haushaltsverbrauchs und der bisherige Tagesmittelwert pausiert. Physische Verbrauchszähler, das Tagesbetriebsdiagramm und die Batteriesteuerung laufen normal weiter. Prognosen verwenden eine konstante Grundlast aus 01:00–05:00 Uhr: Eine Nacht gilt ab 3 Stunden Abdeckung; verwendet wird der Median der bis zu drei letzten gültigen Nächte. Schalte AUS, um das Lernen fortzusetzen; Urlaubsdaten bleiben vom Recorder-Backfill ausgeschlossen.",
    battery_manual_mode: "Wenn EIN, wird diese Batterie einmal auf 0 W gesetzt und aus der automatischen Regelung genommen. Ihr manueller Modus und ihre Sollwerte können danach gewählt werden; andere Batterien laufen automatisch weiter. Omnibattery-Softwaregrenzen wirken nicht, die eigenen BMS-/Treiber-Schutzfunktionen jedoch schon. Der globale manuelle Modus ist unabhängig.",
    fronius_internal_control_disabled: "Wenn EIN (empfohlen), behält Omnibattery die externe Speichersteuerung mit einem 0/0-Leerlauffenster bei Einrichtung, Neuladen und geordnetem Herunterfahren. AUS gibt die Steuerung ausdrücklich an Fronius zurück und entfernt BYD aus dem automatischen Pool.",
    secWeeklyFull: "Wähle den Wochentag, an dem die Batterien zum Zellausgleich auf 100% geladen werden. Nach Erreichen von 100% kehrt das System zum konfigurierten maximalen Ladelimit zurück.",
    secSlots: "Lege fest, wann und wie die Batterien arbeiten dürfen. Die Häkchen steuern jede Richtung, SOC und Leistung. Der manuelle Modus erzwingt eine exakte Leistung und umgeht den PD-Algorithmus.",
    secExcluded: "Geräte mit spezieller Verwaltung konfigurieren: Du kannst Geräte AUSSCHLIESSEN, die NICHT von der Batterie versorgt werden sollen, oder Geräte HINZUFÜGEN, die von der Batterie versorgt werden SOLLEN, auch wenn sie nicht im Hausverbrauchssensor erfasst sind.",
    secCommon: "Systemweite Steuerung. Der Urlaubsmodus pausiert das Lernen des Haushaltsverbrauchs, ohne Messung oder Batteriesteuerung anzuhalten. Die übrigen Parameter werden vom PD-Regler und der direkten Nachführung ohne PD gemeinsam genutzt; Änderungen wirken auf den aktiven Modus.",
    secPd: "Erweiterte PD-Reglerparameter für die Experten-Abstimmung des Lade-/Entladeverhaltens konfigurieren. Ändere diese nur, wenn du die PID-Regelungstheorie verstehst. Die Standardwerte funktionieren für die meisten Installationen gut.",
    secNoPd: "Wenn EIN, wird der PD-Regler umgangen und jede Batterie folgt dem Netz-Sollwert 1:1 (roh, kp=1, ohne Integral/Differential/Glättung/Änderungsbegrenzung). Totzone, min. Lade-/Entladeleistung, Relais-Mindestlaufzeit und Ziel-Netzleistung von oben werden weiterhin genutzt. Nur verwenden, wenn die PD-Abstimmung deinen Zähler nicht bändigen kann; PD ist der sicherere Standard.",
    no_pd_command_delay: "Debounce-Fenster für den No-PD-Modus. Netz-Sensor-Updates innerhalb dieses Fensters werden zu einem einzigen Befehl mit dem neuesten Wert zusammengefasst, damit ein schneller Zähler den Bus nicht überflutet. 0 = bei jedem Ereignis handeln (nur durch das PD-Min.-Zyklusintervall begrenzt). Bereich: 0–3 s, Schritt 0,1, Standard: 0 s.",
    diagPredictive: "Lädt die Batterien während der Nebenzeiten aus dem Netz, wenn die heutige Solarprognose nicht ausreicht.",
    diagChargeDelay: "Verzögert das Laden der Batterien, bis die solare Energiebilanz es erfordert, und exportiert den Solarüberschuss in der Zwischenzeit ins Netz.",
    secHourly: "Erfasst Netzimport/-export pro Stunde und passt den Batterie-Sollwert automatisch an, um eine Ziel-Nettoenergiebilanz zu erreichen.\n\n⚠️ Nur in Spanien sinnvoll, im Rahmen der stündlichen Überschussvergütung (RD 244/2019), bei der Netzüberschuss stundenweise abgerechnet wird. In Märkten mit Einspeisevergütung oder jährlichem Netzausgleich bietet sie keinen Nutzen und kann zu Einnahmeverlusten bei der Einspeisung und unnötigen Batteriezyklen führen.",
    diagPeak: "Wenn aktiviert und der Batterie-SOC unter einen Schwellenwert fällt, spart das System Energie, indem es nur entlädt, um Verbrauch über einem Spitzenlimit auszugleichen.",
    secSysLimits: "Wenn aktiviert, begrenzen die beiden Schieberegler unten die kombinierte Lade-/Entladeleistung aller aktiven Batterien.",
    excluded_device_enabled: "✓ AKTIVIERT = Hausverbrauchssensor erfasst dieses Gerät BEREITS → Batterie versorgt es NICHT (ausgeschlossen). ✗ DEAKTIVIERT = Hausverbrauchssensor erfasst es nicht → Batterie versorgt es (zusätzlich)",
    excluded_device_solar_surplus: "Wenn aktiviert, kann das Gerät Energie direkt von den Solarmodulen (Überschuss) beziehen, ohne dass die Batterie versucht auszugleichen. Empfohlen für Geräte mit hohem Verbrauch wie EV-Ladegeräte.",
    excluded_device_dynamic_power_control: "Für Geräte, die ihren Bedarf über einen Netzzähler dynamisch regeln. Benötigt Solarüberschuss und einen Aktivitäts-/EV-Ladestatussensor, der Vorrang anfordert, bevor Leistung erscheint. Echter Restüberschuss kann weiterhin die Batterie laden.",
    excluded_device_cover_home: "Wenn aktiviert (benötigt Solarüberschuss + Solarsensor), deckt die Batterie den Eigenverbrauch des Hauses, während dieses Gerät läuft, und bezieht Netzstrom nur für das Gerät selbst. Wenn deaktiviert, bleibt die Batterie inaktiv, solange das Gerät aktiv ist.",
    weekly_full_charge_enabled: "Wenn EIN, laden die Batterien einmal pro Woche (unten gewählter Tag) auf 100% zum Zellausgleich und kehren dann zum konfigurierten max. SOC zurück.",
    dp_price_discharge_control: "Wenn EIN, entlädt die Batterie nur, wenn der aktuelle Preis über dem Max-Schwellenwert liegt (oder dem automatischen Tagesdurchschnitt, falls nicht gesetzt). Wenn Zeitfenster die Entladung einschränken, müssen beide Bedingungen erfüllt sein.",
    reevaluate_dynamic_pricing: "Erstellt den heutigen dynamischen Preis-Ladeplan sofort neu, mit den aktuellsten Preisen und der Solarprognose, ohne auf den automatischen Tageslauf zu warten.",
    rt_price_discharge_control: "Wenn EIN, entlädt die Batterie nur, wenn der aktuelle Preis über dem Schwellenwert liegt (fest oder Tagesdurchschnitt). Wenn Zeitfenster die Entladung einschränken, müssen beide Bedingungen erfüllt sein.",
    hourly_balance_target_net_wh: "Ziel-Netto-Netzenergie pro Stunde. 0 = neutral (kein Netto-Import/Export). Positiv = so viel importieren; negativ = exportieren. Bereich -2 bis 2 kWh.",
    hourly_balance_max_offset_w: "Maximale Leistungsanpassung, die der Stundenausgleich auf den Batterie-Sollwert anwenden darf. Höher = schnellere, aber aggressivere Korrektur. Bereich 100–5000 W.",
    hourly_balance_deadband_wh: "Netto-Energie-Totband. Bleibt die Abweichung der Stunde vom Ziel innerhalb dieses Bandes, erfolgt keine Korrektur. Bereich 0–0,5 kWh.",
    hourly_balance_hysteresis_w: "Minimale Änderung des berechneten Offsets, bevor der Sollwert aktualisiert wird, um Jitter zu vermeiden. Bereich 0–200 W.",
    weekly_full_charge_day: "Tag, an dem die Batterien unabhängig vom konfigurierten maximalen SOC auf 100% geladen werden. Dies hilft beim Ausgleich der Batteriezellen.",
    pd_tuning_profile: "PD-Presets per Klick, von sanft bis schnell. Setzt Kp, Kd und max. Leistungsänderung zusammen (Totband bleibt separat). Das Bewegen eines dieser Regler wechselt zu Benutzerdefiniert. Sanfter = ruhiger aber langsamer; aggressiver = schneller, kann aber überschwingen.",
    system_pd_control_quality: "Wie gut der PD das Netzziel hält. Stabil = gut; Schwingend = Pendeln (sanfteres Profil oder größeres Totband); Träge = zu langsam (aggressiveres Profil); Batteriebegrenzt = Batterie voll/leer, kein Abstimmungsproblem. Nach einer Änderung 1-2 Min warten.",
    pd_controller_kp: "Reaktionsfähigkeit auf Netzungleichgewicht. Höhere Werte = schnellere Reaktion, aber Überschwinggefahr. Bereich: 0.1-2.0, Standard: 0.35",
    pd_controller_kd: "Dämpfung zur Vermeidung von Schwingungen. Höhere Werte = sanftere Übergänge, aber langsameres Einschwingen. Bereich: 0.0-2.0, Standard: 0.3",
    pd_controller_deadband: "Netzleistungstoleranz um Null. Verhindert Mikroanpassungen bei kleinen Schwankungen. Höhere Werte verringern die Empfindlichkeit. Bereich: 0-200W, Standard: 40W",
    pd_controller_max_power_change: "Maximale Batterieleistungsänderung pro Regelzyklus (2,5s). Verhindert abrupte Befehle. Niedrigere Werte = sanfter, aber langsamer. Bereich: 100-2000W, Standard: 800W",
    pd_controller_direction_hysteresis: "Leistungsschwelle, um zwischen Laden und Entladen zu wechseln. Verhindert schnelle Richtungswechsel. Bereich: 0-200W, Standard: 60W",
    pd_min_charge_power: "Mindestleistung zum Laden. Unter diesem Schwellenwert bleibt der Regler im Leerlauf, statt mit niedriger Leistung zu laden. 0 = deaktiviert.",
    pd_min_discharge_power: "Mindestleistung zum Entladen. Unter diesem Schwellenwert bleibt der Regler im Leerlauf, statt mit niedriger Leistung zu entladen. 0 = deaktiviert.",
    pd_relay_cooldown: "Anti-Klappern: Sobald die Batterie einschaltet, bleibt sie mindestens diese Zeit aktiv, bevor sie in den Leerlauf zurückkehrt, damit das Relais nicht schaltet, wenn das Netz während der Solar-Rampe (Sonnenauf-/-untergang) am Totband-Rand pendelt. Während des Haltens läuft sie mit der PD-Min.-Lade-/Entladeleistung (oder 100 W bei 0). Große Ungleichgewichte umgehen sie. 0 = deaktiviert.",
    pd_min_cycle_interval: "Mindestabstand zwischen ereignisgesteuerten Regelzyklen. Netz-Sensor-Updates, die früher eintreffen, werden verworfen, damit ein schneller Zähler langsame Modbus-Bridges (z. B. Elfin EW11) nicht mit Schreib-Bursts überflutet. Der 2-s-Sicherheitstimer wird nie blockiert. 0 = deaktiviert.",
    pd_target_grid_power: "Netzleistungs-Sollwert, auf den der Regler regelt. Positiv = Bezug aus dem Netz (Batterie lädt), negativ = Einspeisung ins Netz (Batterie entlädt), 0 = Nettonull. Der Bereich richtet sich nach der insgesamt konfigurierten Batterieleistung, begrenzt durch die System-Leistungsgrenzen, wenn diese aktiv sind. Standard: 0 W.",
    system_max_charge_power: "Optionale Begrenzung der kombinierten Ladeleistung aller aktiven Batterien. 0 = deaktiviert; Limits pro Batterie gelten weiterhin.",
    system_max_discharge_power: "Optionale Begrenzung der kombinierten Entladeleistung aller aktiven Batterien. 0 = deaktiviert; Limits pro Batterie gelten weiterhin.",
    max_contracted_power: "Gesamte Vertragsleistung (ICP) in Watt. Das System überschreitet dieses Limit beim Laden nicht, um ein Auslösen des Leitungsschutzschalters zu vermeiden.",
    predictive_safety_margin_kwh: "Zusätzlicher Puffer der Solarprognose, der sowohl die Ladeentscheidung als auch den für den Abregelungsschutz vorbereiteten Speicherplatz beeinflusst. 0 zum Deaktivieren (Standard). Auf die Gesamtkapazität der Batterie begrenzt.",
    predictive_grid_charge_margin_pct: "Zusätzlicher Prozentsatz, der über das Solar-Defizit hinaus aus dem Netz geladen wird, um optimistische Solarprognosen oder schlechteres Wetter abzufedern. Beispiel: ein Netzbedarf von 2 kWh lädt bei 50 % 3 kWh. 0 zum Deaktivieren (Standard). Auf die Lücke bis zum max. SOC begrenzt.",
    min_soc_floor_enabled: "Hauptschalter für den garantierten Mindest-SOC. Wenn aktiviert, hält die nächtliche Netzladung den unten eingestellten SOC-Boden ein; wenn deaktiviert, wird der Boden ignoriert und die Ladung folgt allein der Solarprognose.",
    predictive_min_soc_floor: "Erzwingt eine nächtliche Netzladung, um bis zum Ende des Ladefensters mindestens diesen SOC zu erreichen, auch wenn die Tagesprognose kein Defizit zeigt. Deckt die Morgenlücke ab, bevor die Solarerzeugung anläuft. 0 zum Deaktivieren (Standard).",
    delay_safety_margin_min: "Stunden vor Sonnenuntergang, bis zu denen das Laden abgeschlossen sein muss. Höhere Werte schalten das Laden früher frei.",
    charge_delay_balance_deadband_kwh: "Toleranz bei der Energiebilanzprüfung. Die Verzögerung wird nur aufgehoben, wenn nutzbare Batterie + Solarprognose den erwarteten Verbrauch um mehr als diesen Wert unterschreiten. Höhere Werte halten die Verzögerung an ausgeglichenen Tagen länger; 0 = bei jedem Defizit freischalten.",
    delay_soc_setpoint_enabled: "Wenn aktiv, lädt die Batterie zuerst bis zum Ziel-SOC, bevor die Solarverzögerung weiteres Laden zurückhält.",
    delay_soc_setpoint: "Der SOC, den die Batterie erreichen muss, bevor die Solarverzögerung greift. Minimum ist 12 % — der minimale Entlade-SOC der Venus-Batterie.",
    capacity_protection_soc_threshold: "Wenn der durchschnittliche Batterie-SOC unter diesen Wert fällt, wird die Kapazitätsschutzfunktion aktiviert. Die Batterie entlädt nicht mehr für den normalen Verbrauch und deckt nur Spitzen über dem Limit ab.",
    capacity_protection_limit: "Netzimport-Leistungsschwelle. Wenn der Hausverbrauch diesen Wert überschreitet und der Schutz aktiv ist, entlädt die Batterie nur den Überschuss über diesem Limit.",
    capacity_protection_excluded_devices: "Wenn aktiviert, deckt die Batterie auch den Anteil ausgeschlossener Lasten, der den Netzbezug über das Spitzenlimit treiben würde. Normale Hausversorgung und Batterieschutz bleiben unverändert.",
    secTempLimit: "Wenn aktiviert, wird die Ladeleistung reduziert, wenn eine Batterie heiß wird: volle Leistung an oder unter der Temperaturgrenze, absinkend bis zum Minimum über den Bereich und wieder ansteigend beim Abkühlen.",
    temp_charge_limit_c: "Die Ladung läuft mit voller Leistung an oder unter dieser Temperatur; darüber beginnt die Drosselung.",
    temp_charge_limit_band_c: "Temperaturbereich oberhalb der Grenze, über den die Ladeleistung bis zum Minimum absinkt.",
    temp_charge_limit_floor_pct: "Ladeleistung an der Grenze plus Bereich, als Prozentsatz der normalen Ladeobergrenze. 0 % stoppt die Ladung bei großer Hitze vollständig.",
    temp_charge_limit_discharge: "Wendet dieselbe temperaturabhängige Drosselung auf die Entladeleistung an. Die Entladung verträgt Hitze besser, daher teilt sie sich als Kompromiss die Ladeschwelle; vor allem hält sie die Entladung unter der harten BMS-Abschaltung.",
    max_price_threshold: "Lade-Obergrenze für dynamische Preise: die Batterie lädt nur aus dem Netz, wenn der Preis auf oder unter diesem Wert liegt. Leer = Tagesdurchschnittspreis. Muss ≤ der Entlade-Untergrenze bleiben.",
    discharge_price_threshold: "Entlade-Untergrenze für dynamische Preise: die Batterie entlädt nur, wenn der Preis auf oder über diesem Wert liegt. Leer = Lade-Obergrenze oder Tagesdurchschnitt. Muss ≥ der Lade-Obergrenze bleiben.",
    min_arbitrage_margin: "Mindestgewinn pro kWh, der vor dem Netzladen erforderlich ist. 0 oder leer = aus. Wenn gesetzt, folgt die Obergrenze der Tagesspreizung: es wird nicht geladen, wenn die teuren Stunden nicht weit genug über den günstigen liegen, um die Umwandlungsverluste zu decken.",
    round_trip_efficiency: "Round-Trip-Wirkungsgrad der Batterie (kWh raus / kWh rein) zur Bewertung gespeicherter Energie. Niedrigere Werte machen den Filter strenger. Nur bei gesetzter Mindest-Arbitragemarge aktiv.",
  },
  fr: {
    secManual: "Quand ACTIVÉ, le contrôle automatique (PD, charge prédictive, plages horaires, écrêtage des pics…) est mis en pause et chaque batterie est réglée à 0 W (repos). DÉSACTIVE-le pour reprendre le contrôle automatique.",
    vacation_mode: "Quand il est ACTIVÉ, l'apprentissage de la consommation du foyer et l'ancienne moyenne journalière sont suspendus. Les compteurs physiques, le graphique d'opération quotidienne et le contrôle des batteries continuent normalement. Les prévisions utilisent une charge de base constante calculée de 01:00 à 05:00 : une nuit est valide avec 3 heures de couverture et la médiane des trois dernières nuits valides au maximum est utilisée. DÉSACTIVE-le pour reprendre l'apprentissage ; les données de vacances restent exclues du backfill Recorder.",
    battery_manual_mode: "Lorsque cette option est activée, cette batterie passe une fois à 0 W et sort du contrôle automatique. Son mode et ses consignes manuels peuvent ensuite être choisis ; les autres batteries continuent en automatique. Les limites logicielles d'Omnibattery ne s'appliquent pas, mais les protections du BMS/driver restent actives. Le mode manuel global est indépendant.",
    fronius_internal_control_disabled: "ACTIVÉ (recommandé) conserve le contrôle externe avec une fenêtre de repos 0/0 pendant la configuration, le rechargement et l'arrêt. DÉSACTIVÉ rend explicitement le contrôle à Fronius et retire BYD du groupe automatique.",
    secWeeklyFull: "Sélectionne le jour de la semaine où les batteries doivent se charger à 100% pour l'équilibrage des cellules. Une fois 100% atteint, le système revient à la limite de charge maximale configurée.",
    secSlots: "Définis quand et comment les batteries sont autorisées à fonctionner. Les cases contrôlent chaque direction, le SOC et la puissance. Le mode manuel force une puissance exacte en contournant l'algorithme PD.",
    secExcluded: "Configure des appareils avec une gestion spéciale : tu peux EXCLURE des appareils qui ne doivent PAS être alimentés par la batterie, ou AJOUTER des appareils qui DOIVENT être alimentés par la batterie même s'ils ne sont pas dans le capteur de consommation domestique.",
    secCommon: "Contrôles généraux du système. Le Mode vacances suspend l'apprentissage de la consommation sans arrêter les mesures physiques ni le contrôle des batteries. Les autres paramètres sont partagés par le régulateur PD et le suivi direct sans PD ; les modifier affecte le mode actif.",
    secPd: "Configure les paramètres avancés du contrôleur PD pour un réglage expert du comportement de charge/décharge des batteries. Ne modifie ces valeurs que si tu comprends la théorie du contrôle PID. Les valeurs par défaut conviennent à la plupart des installations.",
    secNoPd: "Quand ACTIVÉ, le régulateur PD est contourné et chaque batterie suit la consigne réseau 1:1 (brut, kp=1, sans intégral/dérivé/lissage/limite de variation). La bande morte, les puissances min. de charge/décharge, la temporisation relais et la puissance cible réseau ci-dessus restent utilisées. À n'utiliser que si le réglage PD ne parvient pas à dompter ton compteur ; PD est la valeur par défaut la plus sûre.",
    no_pd_command_delay: "Fenêtre de regroupement (debounce) pour le mode sans PD. Les mises à jour du capteur réseau arrivant dans cette fenêtre sont regroupées en une seule commande émise avec la dernière valeur, pour qu'un compteur rapide n'inonde pas le bus. 0 = agir à chaque événement (limité uniquement par l'intervalle min. de cycle PD). Plage : 0–3 s, pas 0,1, défaut : 0 s.",
    diagPredictive: "Charge les batteries depuis le réseau pendant les heures creuses lorsque la prévision solaire du jour est insuffisante.",
    diagChargeDelay: "Retarde la charge des batteries jusqu'à ce que le bilan énergétique solaire l'indique nécessaire, en exportant l'excédent solaire vers le réseau entre-temps.",
    secHourly: "Suit l'import/export réseau par heure et ajuste automatiquement la consigne de la batterie pour atteindre un bilan énergétique net cible.\n\n⚠️ Utile uniquement en Espagne, dans le cadre du régime de compensation horaire des surplus (RD 244/2019), où le surplus injecté sur le réseau est réglé heure par heure. Sur les marchés avec tarif de rachat (feed-in) ou bilan net annuel, elle n'offre aucun avantage et peut entraîner une perte de revenus d'injection et des cycles de batterie inutiles.",
    diagPeak: "Si activé, lorsque le SOC de la batterie descend sous un seuil, le système conserve l'énergie en ne déchargeant que pour compenser la consommation au-dessus d'une limite de pic.",
    secSysLimits: "Si activé, les deux curseurs ci-dessous plafonnent la puissance combinée de charge/décharge de toutes les batteries actives.",
    excluded_device_enabled: "✓ COCHÉ = Le capteur domestique inclut DÉJÀ cet appareil → La batterie ne l'alimentera PAS (exclu). ✗ DÉCOCHÉ = Le capteur domestique ne le voit pas → La batterie l'alimentera (additionnel)",
    excluded_device_solar_surplus: "Si coché, l'appareil pourra consommer l'énergie directement des panneaux solaires (excédent) sans que la batterie tente de compenser. Recommandé pour les appareils à forte consommation comme les chargeurs de VE.",
    excluded_device_dynamic_power_control: "Pour les appareils qui ajustent dynamiquement leur demande à partir d'un compteur réseau. Nécessite Surplus Solaire et un capteur d'activité / charge VE, qui demande la priorité avant l'apparition de puissance. Le surplus réellement restant peut encore charger la batterie.",
    excluded_device_cover_home: "Si activé (nécessite Surplus Solaire + capteur solaire), la batterie couvre la consommation propre de la maison pendant que cet appareil fonctionne, n'important du réseau que pour l'appareil. Si désactivé, la batterie reste inactive tant que l'appareil est actif.",
    weekly_full_charge_enabled: "Si activé, les batteries se chargent à 100% un jour par semaine (choisi ci-dessous) pour équilibrer les cellules, puis reviennent au SOC max configuré.",
    dp_price_discharge_control: "Si activé, la batterie ne se décharge que lorsque le prix actuel dépasse le seuil maximum (ou la moyenne journalière automatique si non défini). Si les plages horaires limitent la décharge, les deux conditions doivent être remplies.",
    reevaluate_dynamic_pricing: "Recalcule immédiatement le planning de charge par prix dynamiques du jour, avec les derniers prix et la prévision solaire, sans attendre l'exécution quotidienne automatique.",
    rt_price_discharge_control: "Si activé, la batterie ne se décharge que lorsque le prix actuel dépasse le seuil (fixe ou moyenne journalière). Si les plages horaires limitent la décharge, les deux conditions doivent être remplies.",
    hourly_balance_target_net_wh: "Énergie réseau nette cible par heure. 0 = neutre (pas d'import/export net). Positif = importer cette quantité ; négatif = exporter. Plage -2 à 2 kWh.",
    hourly_balance_max_offset_w: "Ajustement de puissance maximal que le bilan horaire peut appliquer au point de consigne. Plus élevé = correction plus rapide mais plus agressive. Plage 100–5000 W.",
    hourly_balance_deadband_wh: "Bande morte d'énergie nette. Si l'écart de l'heure par rapport à la cible reste dans cette bande, aucune correction n'est appliquée. Plage 0–0,5 kWh.",
    hourly_balance_hysteresis_w: "Variation minimale du décalage calculé avant de mettre à jour le point de consigne, pour éviter les oscillations. Plage 0–200 W.",
    weekly_full_charge_day: "Jour où les batteries se chargeront à 100% quel que soit le SOC maximum configuré. Cela aide à équilibrer les cellules de la batterie.",
    pd_tuning_profile: "Presets PD en un clic, du plus doux au plus rapide. Règle Kp, Kd et le changement de puissance max. ensemble (la bande morte reste séparée). Bouger l'un de ces curseurs passe en Personnalisé. Plus doux = plus calme mais plus lent ; plus agressif = plus rapide mais peut dépasser.",
    system_pd_control_quality: "À quel point le PD tient la cible réseau. Stable = bon ; Oscillant = pompage (essaie un profil plus doux ou une bande morte plus large) ; Lent = trop lent (essaie un profil plus agressif) ; Limité par batterie = batterie pleine/vide, pas un problème de réglage. Attends 1-2 min après un changement.",
    pd_controller_kp: "Réactivité au déséquilibre réseau. Valeurs plus élevées = réponse plus rapide mais risque de dépassement. Plage : 0.1-2.0, défaut : 0.35",
    pd_controller_kd: "Amortissement pour éviter les oscillations. Valeurs plus élevées = transitions plus douces mais stabilisation plus lente. Plage : 0.0-2.0, défaut : 0.3",
    pd_controller_deadband: "Tolérance de puissance réseau autour de zéro. Évite les micro-ajustements face aux fluctuations mineures. Des valeurs plus élevées réduisent la sensibilité. Plage : 0-200W, défaut : 40W",
    pd_controller_max_power_change: "Changement maximal de puissance de batterie par cycle de contrôle (2,5s). Évite les commandes abruptes. Valeurs plus basses = plus doux mais plus lent. Plage : 100-2000W, défaut : 800W",
    pd_controller_direction_hysteresis: "Seuil de puissance requis pour basculer entre charge et décharge. Évite les changements de direction rapides. Plage : 0-200W, défaut : 60W",
    pd_min_charge_power: "Puissance minimale pour charger. En dessous de ce seuil, le contrôleur reste au repos au lieu de charger à faible puissance. 0 = désactivé.",
    pd_min_discharge_power: "Puissance minimale pour décharger. En dessous de ce seuil, le contrôleur reste au repos au lieu de décharger à faible puissance. 0 = désactivé.",
    pd_relay_cooldown: "Anti-claquement : une fois la batterie engagée, elle reste active au moins ce temps avant de revenir au repos, pour que le relais ne commute pas quand le réseau oscille au bord de la bande morte pendant la rampe solaire (lever/coucher). Pendant le maintien, elle fonctionne à la puissance min. de charge/décharge PD (ou 100 W si 0). Les grands déséquilibres l'ignorent. 0 = désactivé.",
    pd_min_cycle_interval: "Espacement minimal entre les cycles de contrôle déclenchés par événement. Les mises à jour du capteur réseau arrivant plus tôt sont ignorées, pour qu'un compteur rapide n'inonde pas les ponts Modbus lents (p. ex. Elfin EW11) de rafales d'écriture. La temporisation de sécurité de 2 s n'est jamais bloquée. 0 = désactivé.",
    pd_target_grid_power: "Consigne de puissance réseau que le contrôleur régule. Positif = soutirage du réseau (la batterie charge), négatif = injection vers le réseau (la batterie décharge), 0 = net zéro. La plage suit la puissance totale configurée de vos batteries, limitée par les limites de puissance système lorsqu'elles sont actives. Défaut : 0 W.",
    system_max_charge_power: "Plafond optionnel pour la puissance de charge combinée de toutes les batteries actives. 0 = désactivé ; les limites par batterie s'appliquent toujours.",
    system_max_discharge_power: "Plafond optionnel pour la puissance de décharge combinée de toutes les batteries actives. 0 = désactivé ; les limites par batterie s'appliquent toujours.",
    max_contracted_power: "Puissance totale souscrite (ICP) en watts. Le système ne dépassera pas cette limite lors de la charge pour éviter de faire disjoncter.",
    predictive_safety_margin_kwh: "Marge supplémentaire de prévision solaire utilisée pour décider de charger et pour préparer l'espace contre l'écrêtement. Mettre à 0 pour désactiver (défaut). Limitée à la capacité totale de la batterie.",
    predictive_grid_charge_margin_pct: "Pourcentage supplémentaire chargé depuis le réseau au-dessus du déficit solaire, pour couvrir des prévisions solaires optimistes ou une météo pire que prévu. Exemple : un besoin réseau de 2 kWh à 50 % charge 3 kWh. Mets 0 pour désactiver (défaut). Plafonné à l'écart jusqu'au SOC max.",
    min_soc_floor_enabled: "Interrupteur principal du SOC minimal garanti. Activé, la charge réseau nocturne respecte le plancher de SOC réglé ci-dessous ; désactivé, le plancher est ignoré et la charge suit uniquement la prévision solaire.",
    predictive_min_soc_floor: "Force une charge réseau nocturne pour atteindre au moins ce SOC à la fin de la fenêtre de charge, même si la prévision solaire de la journée n'indique aucun déficit. Couvre le creux matinal avant la montée du solaire. Mets 0 pour désactiver (défaut).",
    delay_safety_margin_min: "Heures avant le coucher du soleil auxquelles la charge doit être terminée. Des valeurs plus élevées débloquent la charge plus tôt.",
    charge_delay_balance_deadband_kwh: "Tolérance sur le calcul du bilan énergétique. Le délai ne se débloque que lorsque batterie utilisable + prévision solaire est inférieure à la consommation attendue de plus que cette valeur. Des valeurs plus élevées maintiennent le délai plus longtemps les jours équilibrés ; 0 = débloquer au moindre déficit.",
    delay_soc_setpoint_enabled: "Si activé, la batterie charge d'abord jusqu'au SOC cible avant que le délai de charge solaire ne retienne la charge.",
    delay_soc_setpoint: "Le SOC que la batterie doit atteindre avant que le délai solaire ne s'active. Le minimum est 12 % — le SOC de décharge minimal de la batterie Venus.",
    capacity_protection_soc_threshold: "Quand le SOC moyen des batteries descend sous cette valeur, l'écrêtage des pics s'active. La batterie cesse de décharger pour la consommation normale et ne couvre que les pics au-dessus de la limite.",
    capacity_protection_limit: "Seuil de puissance d'import réseau. Quand la consommation de la maison dépasse cette valeur et que la protection est active, la batterie ne décharge que l'excédent au-dessus de cette limite.",
    capacity_protection_excluded_devices: "Si activé, la batterie couvre aussi la part de la demande exclue qui ferait dépasser la limite de pointe du réseau. La couverture normale du foyer et les protections de la batterie restent inchangées.",
    secTempLimit: "Lorsque activé, la puissance de charge est réduite quand une batterie chauffe : pleine puissance à la limite de température ou en dessous, diminuant jusqu'au minimum sur la plage, puis remontant au refroidissement.",
    temp_charge_limit_c: "La charge fonctionne à pleine puissance à cette température ou en dessous ; au-dessus, la réduction commence.",
    temp_charge_limit_band_c: "Plage de température au-dessus de la limite sur laquelle la puissance de charge diminue jusqu'au minimum.",
    temp_charge_limit_floor_pct: "Puissance de charge à la limite plus la plage, en pourcentage du plafond de charge normal. 0 % arrête complètement la charge en cas de forte chaleur.",
    temp_charge_limit_discharge: "Applique la même réduction liée à la température à la puissance de décharge. La décharge tolère mieux la chaleur, elle partage donc le seuil de charge par compromis ; surtout, elle maintient la décharge sous la coupure dure du BMS.",
    max_price_threshold: "Plafond de charge pour la tarification dynamique : la batterie ne charge depuis le réseau que si le prix est à ce niveau ou en dessous. Vide = prix moyen journalier. Doit rester ≤ au plancher de décharge.",
    discharge_price_threshold: "Plancher de décharge pour la tarification dynamique : la batterie ne décharge que si le prix est à ce niveau ou au-dessus. Vide = plafond de charge ou prix moyen journalier. Doit rester ≥ au plafond de charge.",
    min_arbitrage_margin: "Profit minimal par kWh exigé avant la charge réseau. 0 ou vide = désactivé. Si défini, le plafond suit l'écart du jour : la charge est ignorée lorsque les heures chères ne dépassent pas assez les heures creuses pour couvrir les pertes de conversion.",
    round_trip_efficiency: "Rendement aller-retour de la batterie (kWh sortis / kWh entrés) servant à valoriser l'énergie stockée. Des valeurs plus basses rendent le filtre plus strict. Utilisé uniquement si une marge d'arbitrage minimale est définie.",
  },
  nl: {
    secManual: "Wanneer AAN, wordt de automatische regeling (PD, voorspellend laden, tijdvensters, piekafvlakking…) gepauzeerd en wordt elke batterij op 0 W (rust) gezet. Zet UIT om de automatische regeling te hervatten.",
    vacation_mode: "Wanneer AAN, worden het leren van het huishoudverbruik en het oude daggemiddelde gepauzeerd. Fysieke verbruiksmeters, de dagelijkse werkinggrafiek en de batterijregeling blijven normaal werken. Prognoses gebruiken een constante basislast uit 01:00–05:00: een nacht is geldig vanaf 3 uur dekking en de mediaan van maximaal de laatste drie geldige nachten wordt gebruikt. Zet UIT om het leren te hervatten; vakantiegegevens blijven uitgesloten van Recorder-backfill.",
    battery_manual_mode: "Als deze optie AAN staat, wordt deze batterij eenmalig op 0 W gezet en uit de automatische regeling gehaald. De handmatige modus en setpoints kunnen daarna worden gekozen; andere batterijen blijven automatisch werken. Softwarelimieten van Omnibattery gelden niet, maar de eigen BMS-/driverbeveiliging wel. De globale handmatige modus staat hier los van.",
    fronius_internal_control_disabled: "AAN (aanbevolen) behoudt externe regeling met een 0/0-rustvenster tijdens configuratie, herladen en afsluiten. UIT geeft de regeling expliciet terug aan Fronius en verwijdert BYD uit de automatische groep.",
    secWeeklyFull: "Selecteer de dag van de week waarop de batterijen tot 100% moeten laden voor celbalancering. Na het bereiken van 100% keert het systeem terug naar de geconfigureerde maximale laadlimiet.",
    secSlots: "Bepaal wanneer en hoe de batterijen mogen werken. De vinkjes regelen elke richting, SOC en vermogen. De handmatige modus forceert een exact vermogen en omzeilt het PD-algoritme.",
    secExcluded: "Configureer apparaten met speciaal beheer: je kunt apparaten UITSLUITEN die NIET door de batterij gevoed mogen worden, of apparaten TOEVOEGEN die WEL door de batterij gevoed moeten worden, ook al staan ze niet in de huisverbruikssensor.",
    secCommon: "Algemene systeembediening. Vakantiemodus pauzeert het leren van huishoudverbruik zonder fysieke meting of batterijregeling te stoppen. De overige parameters worden gedeeld door de PD-regelaar en directe tracking zonder PD; wijzigingen beïnvloeden de actieve modus.",
    secPd: "Configureer geavanceerde PD-regelaarparameters voor het expert-afstemmen van het laad-/ontlaadgedrag. Wijzig deze alleen als je de PID-regeltheorie begrijpt. De standaardwaarden werken goed voor de meeste installaties.",
    secNoPd: "Wanneer AAN wordt de PD-regelaar omzeild en volgt elke batterij het net-setpoint 1:1 (ruw, kp=1, zonder integraal/afgeleide/afvlakking/snelheidslimiet). De dode band, min. laad-/ontlaadvermogen, relais-wachttijd en doelnetvermogen hierboven blijven in gebruik. Gebruik dit alleen als PD-afstemming je meter niet kan temmen; PD is de veiligere standaard.",
    no_pd_command_delay: "Debounce-venster voor de No-PD-modus. Net-sensorupdates die binnen dit venster binnenkomen worden samengevoegd tot één commando met de laatste waarde, zodat een snelle meter de bus niet overspoelt. 0 = bij elke gebeurtenis handelen (alleen begrensd door het PD-min.-cyclusinterval). Bereik: 0–3 s, stap 0,1, standaard: 0 s.",
    diagPredictive: "Laadt de batterijen uit het net tijdens daluren wanneer de zonneprognose van vandaag onvoldoende is.",
    diagChargeDelay: "Stelt het laden van de batterijen uit totdat de zonne-energiebalans aangeeft dat het nodig is, en exporteert ondertussen het zonneoverschot naar het net.",
    secHourly: "Volgt netimport/-export per uur en past het batterij-setpoint automatisch aan om een gewenste netto-energiebalans te bereiken.\n\n⚠️ Alleen nuttig in Spanje, onder de regeling voor uurlijkse compensatie van overschotten (RD 244/2019), waarbij netoverschot per uur wordt verrekend. In markten met terugleververgoeding (feed-in) of jaarlijkse saldering biedt het geen voordeel en kan het leiden tot gemiste teruglever-inkomsten en onnodige batterijcycli.",
    diagPeak: "Indien ingeschakeld bespaart het systeem energie wanneer de batterij-SOC onder een drempel zakt, door alleen te ontladen om verbruik boven een pieklimiet te compenseren.",
    secSysLimits: "Indien ingeschakeld begrenzen de twee schuifregelaars hieronder het gecombineerde laad-/ontlaadvermogen van alle actieve batterijen.",
    excluded_device_enabled: "✓ AANGEVINKT = Huissensor bevat dit apparaat AL → Batterij voedt het NIET (uitgesloten). ✗ NIET AANGEVINKT = Huissensor ziet het niet → Batterij voedt het WEL (aanvullend)",
    excluded_device_solar_surplus: "Indien aangevinkt kan het apparaat energie rechtstreeks van de zonnepanelen (overschot) verbruiken zonder dat de batterij probeert te compenseren. Aanbevolen voor apparaten met hoog verbruik zoals EV-laders.",
    excluded_device_dynamic_power_control: "Voor apparaten die hun vraag dynamisch via een netmeter regelen. Vereist Zonne-overschot en een activiteit-/EV-laadsensor, die voorrang vraagt voordat vermogen verschijnt. Echt resterend overschot kan de batterij nog steeds laden.",
    excluded_device_cover_home: "Indien AAN (vereist Zonne-overschot + zonnesensor) dekt de batterij het eigen huisverbruik terwijl dit apparaat draait en importeert alleen netstroom voor het apparaat zelf. Indien UIT blijft de batterij inactief zolang het apparaat actief is.",
    weekly_full_charge_enabled: "Indien AAN laden de batterijen één dag per week (hieronder gekozen) tot 100% voor celbalancering en keren daarna terug naar de geconfigureerde max. SOC.",
    dp_price_discharge_control: "Indien AAN ontlaadt de batterij alleen wanneer de huidige prijs boven de max. drempel ligt (of het automatische daggemiddelde indien niet ingesteld). Als tijdslots het ontladen beperken, moeten beide voorwaarden gelden.",
    reevaluate_dynamic_pricing: "Herberekent nu meteen het laadschema op basis van dynamische prijzen van vandaag, met de meest recente prijzen en zonneprognose, zonder te wachten op de automatische dagelijkse uitvoering.",
    rt_price_discharge_control: "Indien AAN ontlaadt de batterij alleen wanneer de huidige prijs boven de drempel ligt (vast of daggemiddelde). Als tijdslots het ontladen beperken, moeten beide voorwaarden gelden.",
    hourly_balance_target_net_wh: "Doel netto netenergie per uur. 0 = neutraal (geen netto import/export). Positief = zoveel importeren; negatief = exporteren. Bereik -2 tot 2 kWh.",
    hourly_balance_max_offset_w: "Maximale vermogensaanpassing die de uurbalans op het batterij-setpoint mag toepassen. Hoger = corrigeert sneller maar agressiever. Bereik 100–5000 W.",
    hourly_balance_deadband_wh: "Dodeband netto-energie. Blijft de afwijking van het uur t.o.v. het doel binnen deze band, dan wordt geen correctie toegepast. Bereik 0–0,5 kWh.",
    hourly_balance_hysteresis_w: "Minimale wijziging in de berekende offset voordat het setpoint wordt bijgewerkt, om jitter te voorkomen. Bereik 0–200 W.",
    weekly_full_charge_day: "Dag waarop batterijen tot 100% worden geladen, ongeacht de geconfigureerde maximale SOC. Dit helpt bij het balanceren van batterijcellen.",
    pd_tuning_profile: "PD-presets met één klik, van zachtst naar snelst. Stelt Kp, Kd en max. vermogensverandering samen in (dode zone blijft apart). Een van die schuifregelaars verplaatsen schakelt naar Aangepast. Zachter = rustiger maar trager; agressiever = sneller maar kan doorschieten.",
    system_pd_control_quality: "Hoe goed de PD het netdoel vasthoudt. Stabiel = goed; Oscillerend = pendelen (gebruik een zachter profiel of grotere dode zone); Traag = te langzaam (gebruik een agressiever profiel); Batterijbegrensd = batterij vol/leeg, geen afstemprobleem. Wacht 1-2 min na een wijziging.",
    pd_controller_kp: "Reactievermogen op netonbalans. Hogere waarden = snellere reactie maar risico op doorschieten. Bereik: 0.1-2.0, standaard: 0.35",
    pd_controller_kd: "Demping om oscillatie te voorkomen. Hogere waarden = vloeiendere overgangen maar langzamere stabilisatie. Bereik: 0.0-2.0, standaard: 0.3",
    pd_controller_deadband: "Netvermogenstolerantie rond nul. Voorkomt micro-aanpassingen bij kleine fluctuaties. Hogere waarden verlagen de gevoeligheid. Bereik: 0-200W, standaard: 40W",
    pd_controller_max_power_change: "Maximale batterijvermogensverandering per regelcyclus (2,5s). Voorkomt abrupte commando's. Lagere waarden = vloeiender maar trager. Bereik: 100-2000W, standaard: 800W",
    pd_controller_direction_hysteresis: "Vermogensdrempel die nodig is om te wisselen tussen laden en ontladen. Voorkomt snelle richtingswisselingen. Bereik: 0-200W, standaard: 60W",
    pd_min_charge_power: "Minimaal vermogen om te laden. Onder deze drempel blijft de regelaar in rust in plaats van met laag vermogen te laden. 0 = uitgeschakeld.",
    pd_min_discharge_power: "Minimaal vermogen om te ontladen. Onder deze drempel blijft de regelaar in rust in plaats van met laag vermogen te ontladen. 0 = uitgeschakeld.",
    pd_relay_cooldown: "Anti-klapperen: zodra de batterij inschakelt, blijft hij minstens deze tijd actief voordat hij naar rust terugkeert, zodat het relais niet schakelt wanneer het net tijdens de zonne-ramp (op-/ondergang) op de rand van de dode band schommelt. Tijdens het vasthouden draait hij op het PD min. laad-/ontlaadvermogen (of 100 W bij 0). Grote onbalans omzeilt het. 0 = uitgeschakeld.",
    pd_min_cycle_interval: "Minimale tussenruimte tussen gebeurtenisgestuurde regelcycli. Net-sensorupdates die eerder binnenkomen worden genegeerd, zodat een snelle meter trage Modbus-bridges (bijv. Elfin EW11) niet overspoelt met schrijfbursts. De 2 s-veiligheidstimer wordt nooit geblokkeerd. 0 = uitgeschakeld.",
    pd_target_grid_power: "Netvermogen-setpoint waarop de regelaar regelt. Positief = afname van het net (batterij laadt), negatief = teruglevering aan het net (batterij ontlaadt), 0 = netto nul. Het bereik volgt het totaal geconfigureerde batterijvermogen, begrensd door de systeemvermogenslimieten wanneer die actief zijn. Standaard: 0 W.",
    system_max_charge_power: "Optionele begrenzing voor het gecombineerde laadvermogen van alle actieve batterijen. 0 = uitgeschakeld; limieten per batterij blijven gelden.",
    system_max_discharge_power: "Optionele begrenzing voor het gecombineerde ontlaadvermogen van alle actieve batterijen. 0 = uitgeschakeld; limieten per batterij blijven gelden.",
    max_contracted_power: "Totaal gecontracteerd vermogen (ICP) in watt. Het systeem overschrijdt deze limiet niet bij het laden om te voorkomen dat de hoofdzekering uitschakelt.",
    predictive_safety_margin_kwh: "Extra buffer op de zonneprognose die zowel de laadbeslissing als de voorbereide ruimte tegen afregeling beïnvloedt. Zet op 0 om uit te schakelen (standaard). Begrensd tot de totale batterijcapaciteit.",
    predictive_grid_charge_margin_pct: "Extra percentage dat boven het zonne-tekort uit het net wordt geladen, om optimistische zonneprognoses of slechter weer op te vangen. Voorbeeld: een netbehoefte van 2 kWh laadt bij 50 % 3 kWh. Zet op 0 om uit te schakelen (standaard). Begrensd tot het gat tot max SOC.",
    min_soc_floor_enabled: "Hoofdschakelaar voor de gegarandeerde minimale SOC. Ingeschakeld houdt de nachtelijke netlading de hieronder ingestelde SOC-ondergrens aan; uitgeschakeld wordt de ondergrens genegeerd en volgt het laden alleen de zonneprognose.",
    predictive_min_soc_floor: "Forceert een nachtelijke netlading om aan het einde van het laadvenster minstens deze SOC te bereiken, ook als de zonneprognose voor de dag geen tekort toont. Dekt het ochtendgat voordat de zon op gang komt. Zet op 0 om uit te schakelen (standaard).",
    delay_safety_margin_min: "Uren voor zonsondergang waarop het laden voltooid moet zijn. Hogere waarden ontgrendelen het laden eerder.",
    charge_delay_balance_deadband_kwh: "Tolerantie op de energiebalanscontrole. De vertraging wordt alleen opgeheven wanneer bruikbare batterij + zonneprognose meer dan deze waarde onder het verwachte verbruik blijft. Hogere waarden houden de vertraging langer vast op evenwichtige dagen; 0 = ontgrendel bij elk tekort.",
    delay_soc_setpoint_enabled: "Indien aan, laadt de batterij eerst tot de doel-SOC voordat de zonnevertraging verder laden tegenhoudt.",
    delay_soc_setpoint: "De SOC die de batterij moet bereiken voordat de zonnevertraging ingaat. Minimum is 12 % — de minimale ontlaad-SOC van de Venus-batterij.",
    capacity_protection_soc_threshold: "Wanneer de gemiddelde batterij-SOC onder deze waarde zakt, wordt capaciteitsbescherming geactiveerd. De batterij stopt met ontladen voor normaal verbruik en dekt alleen pieken boven de limiet.",
    capacity_protection_limit: "Netimport-vermogensdrempel. Wanneer het huisverbruik deze waarde overschrijdt en de bescherming actief is, ontlaadt de batterij alleen het overschot boven deze limiet.",
    capacity_protection_excluded_devices: "Indien ingeschakeld dekt de batterij ook het deel van uitgesloten verbruik dat de netafname boven de pieklimiet zou brengen. Normale huisdekking en batterijbeveiligingen blijven ongewijzigd.",
    secTempLimit: "Indien ingeschakeld wordt het laadvermogen verlaagd als een batterij warm wordt: vol vermogen op of onder de temperatuurlimiet, aflopend tot het minimum over de band en weer oplopend bij afkoelen.",
    temp_charge_limit_c: "Laden gebeurt op vol vermogen op of onder deze temperatuur; daarboven begint de terugregeling.",
    temp_charge_limit_band_c: "Temperatuurbereik boven de limiet waarover het laadvermogen tot het minimum afbouwt.",
    temp_charge_limit_floor_pct: "Laadvermogen op de limiet plus de band, als percentage van het normale laadplafond. 0 % stopt het laden volledig bij grote hitte.",
    temp_charge_limit_discharge: "Past dezelfde temperatuurterugregeling toe op het ontlaadvermogen. Ontladen verdraagt warmte beter, dus deelt het als compromis de laaddrempel; vooral houdt het het ontladen onder de harde BMS-uitschakeling.",
    max_price_threshold: "Laadplafond voor dynamische prijzen: de batterij laadt alleen van het net wanneer de prijs op of onder deze waarde ligt. Leeg = daggemiddelde prijs. Moet ≤ de ontlaadondergrens blijven.",
    discharge_price_threshold: "Ontlaadondergrens voor dynamische prijzen: de batterij ontlaadt alleen wanneer de prijs op of boven deze waarde ligt. Leeg = laadplafond of daggemiddelde. Moet ≥ het laadplafond blijven.",
    min_arbitrage_margin: "Minimale winst per kWh die vereist is voordat er van het net geladen wordt. 0 of leeg = uit, dan beslist alleen het laadplafond. Ingesteld laat het plafond de dagspreiding volgen: er wordt niet geladen wanneer de dure uren niet ver genoeg boven de goedkope liggen om de omzettingsverliezen terug te verdienen.",
    round_trip_efficiency: "Retourrendement van de batterij (kWh eruit / kWh erin) om opgeslagen energie te waarderen. Lagere waarden maken het filter strenger. Alleen gebruikt als er een minimale arbitragemarge is ingesteld.",
  },
};

class MarstekVenusPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._panelConfig = {};
    this._built = false;
    this._view = "resumen";
    this._arrangeMode = false; // Control tab: drag-to-reorder cards (sticky)
    this._r = {}; // dynamic node refs for patch-in-place
    this._edgeSig = {}; // per flow edge: last dot signature
    this._socSeries = []; // SOC % samples for the sparkline (history seed + live)
    this._dailyOperationSocHistory = Array(96).fill(null);
    this._dailyOperationSocHistoryDate = null;
    this._socLastPush = 0; // last live-append timestamp (s), to throttle pushes
    this._powerSeries = null; // { t:[...], solar/home/grid/battery:[...] } kW, 24h
    this._weekly = null; // { days:[..7], charge/discharge/import/export:[..7] } kWh
    this._histTimer = null;
    this._historyRefresh = null;
    this._historyStarted = false;
    this._dailyOperationGlobalListenersAttached = false;
    this._dailyOperationVisualViewport = null;
    this._onDailyOperationViewportChange = this._dismissDailyOperationFloatingTooltip.bind(this);
  }

  // --- HA-injected properties ------------------------------------------------
  set hass(hass) {
    this._hass = hass;
    this._applyTheme();
    this._update();
    this._ensureHistoryStarted();
  }
  get hass() {
    return this._hass;
  }
  set panel(panel) {
    this._panelConfig = (panel && panel.config) || {};
    // Home Assistant may assign/refresh `panel` after `hass`. Recompute now so
    // payload-backed sources (notably excluded devices) do not remain hidden
    // until an unrelated entity state update happens to arrive.
    if (this._hass) this._update();
  }
  set narrow(v) {
    this._narrow = v;
  }
  set route(_v) { }

  connectedCallback() {
    this._injectFonts();
    this._update();
    this._attachDailyOperationGlobalListeners();
    this._ensureHistoryStarted();
  }
  disconnectedCallback() {
    this._detachDailyOperationGlobalListeners();
    if (this._histTimer) clearInterval(this._histTimer);
    this._histTimer = null;
    this._historyStarted = false;
  }

  // --- config / theme --------------------------------------------------------
  _domain() {
    return this._panelConfig.domain || FALLBACK_DOMAIN;
  }
  _title() {
    return this._panelConfig.title || FALLBACK_TITLE;
  }
  _lang() {
    return (this._hass && this._hass.locale && this._hass.locale.language) || "es-ES";
  }
  /** Time zone selected in the HA user profile. `local` deliberately leaves
   *  Intl on the browser zone; `server` follows Home Assistant's configured
   *  IANA zone even when the browser or host runs in UTC. */
  _timeZone() {
    const locale = this._hass && this._hass.locale;
    if (!locale || locale.time_zone === "local") return undefined;
    return this._hass && this._hass.config && this._hass.config.time_zone;
  }
  /** Whether clocks should use AM/PM, following the HA profile preference. */
  _useAmPm() {
    const locale = this._hass && this._hass.locale;
    const preference = locale && locale.time_format;
    if (preference === "12") return true;
    if (preference === "24") return false;
    // HA's `language` setting follows the selected UI language, while
    // `system` deliberately asks Intl for the browser/OS default.
    const language = preference === "system" ? undefined : this._lang();
    return new Date("January 1, 2023 22:00:00")
      .toLocaleString(language)
      .includes("10");
  }
  _dateTimeOptions(options = {}) {
    let resolved = options;
    if (Object.prototype.hasOwnProperty.call(options, "hour")
        && options.hourCycle == null && options.hour12 == null) {
      resolved = { ...options, hourCycle: this._useAmPm() ? "h12" : "h23" };
    }
    const timeZone = this._timeZone();
    return timeZone ? { ...resolved, timeZone } : resolved;
  }
  /** Calendar fields for an instant in the time zone shown by Home Assistant. */
  _dateParts(epochMs = Date.now()) {
    const formatter = new Intl.DateTimeFormat("en-CA", this._dateTimeOptions({
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hourCycle: "h23",
    }));
    return Object.fromEntries(
      formatter.formatToParts(new Date(epochMs))
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, Number(part.value)])
    );
  }
  /** Convert a wall-clock midnight in an IANA zone to its real UTC instant. */
  _zonedMidnight(year, month, day) {
    const timeZone = this._timeZone();
    if (!timeZone) return new Date(year, month - 1, day).getTime();
    const targetAsUtc = Date.UTC(year, month - 1, day);
    const offsetAt = (epochMs) => {
      const p = this._dateParts(epochMs);
      return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - epochMs;
    };
    let instant = targetAsUtc - offsetAt(targetAsUtc);
    // Re-evaluate at the candidate so transitions near the target use the
    // offset that actually applies to that local date.
    instant = targetAsUtc - offsetAt(instant);
    return instant;
  }
  /** Start of the selected HA calendar day, optionally shifted by N days. */
  _dayStartEpoch(offsetDays = 0, epochMs = Date.now()) {
    const p = this._dateParts(epochMs);
    const shifted = new Date(Date.UTC(p.year, p.month - 1, p.day + offsetDays));
    return this._zonedMidnight(
      shifted.getUTCFullYear(), shifted.getUTCMonth() + 1, shifted.getUTCDate()
    );
  }
  _localHour(epochMs = Date.now()) {
    return this._dateParts(epochMs).hour;
  }
  /** Two-letter UI language for i18n lookups ("es-ES" -> "es"). */
  _lang2() {
    return String(this._lang()).split("-")[0].toLowerCase();
  }
  /** Localized panel string by key. Falls back es/de/fr/nl -> en -> key.
   *  `vars` fills {name} placeholders. */
  _t(key, vars) {
    const dict = I18N[this._lang2()] || I18N.en;
    let s = dict[key] != null ? dict[key] : I18N.en[key] != null ? I18N.en[key] : key;
    if (vars) for (const k in vars) s = s.replace("{" + k + "}", vars[k]);
    return s;
  }
  /** Options-flow help text for a section tk or entity key. UI language, es/.. ->
   *  en fallback. "" when none. Bold markdown (**) stripped for plain tooltips. */
  _help(key) {
    const dict = SYS_HELP[this._lang2()] || SYS_HELP.en;
    const s = dict[key] != null ? dict[key] : SYS_HELP.en[key];
    return s != null ? String(s).replace(/\*\*/g, "") : "";
  }
  _applyTheme() {
    const dark = !this._hass || !this._hass.themes || this._hass.themes.darkMode !== false;
    this.setAttribute("data-theme", dark ? "dark" : "light");
  }
  _injectFonts() {
    if (document.getElementById("mvem-fonts")) return;
    const l = document.createElement("link");
    l.id = "mvem-fonts";
    l.rel = "stylesheet";
    l.href =
      "https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap";
    document.head.appendChild(l);
  }

  // --- entity resolution -----------------------------------------------------
  /** Index this integration's entities by translation_key and by device. */
  _index() {
    const hass = this._hass;
    const domain = this._domain();
    const reg = hass.entities || {};
    const byKey = new Map(); // translation_key -> [entity_id]
    const byDevice = new Map(); // device_id -> [entity_id]

    for (const e of Object.values(reg)) {
      if (e.platform !== domain || e.hidden) continue;
      const tk = e.translation_key;
      if (tk) {
        if (!byKey.has(tk)) byKey.set(tk, []);
        byKey.get(tk).push(e.entity_id);
      }
      const dev = e.device_id || "_";
      if (!byDevice.has(dev)) byDevice.set(dev, []);
      byDevice.get(dev).push(e.entity_id);
    }
    return { byKey, byDevice };
  }

  _statesFor(byKey, key) {
    const ids = byKey.get(key) || [];
    return ids.map((id) => this._hass.states[id]).filter(Boolean);
  }
  _stateFor(byKey, key) {
    return this._statesFor(byKey, key)[0] || null;
  }
  /** First system/aggregate entity_id for a translation_key (for more-info). */
  _sysEntityId(key) {
    const { byKey } = this._index();
    return (byKey.get(key) || [])[0] || null;
  }
  /** Resolve the home consumption entity_id. Uses the panel config entity if it
   *  still exists in hass.states (survives entity renames that happen after the
   *  integration last loaded), otherwise falls back to the stable translation_key. */
  _homeEntityId(hass) {
    const cfgId = this._panelConfig.home_entity;
    if (cfgId && hass && hass.states[cfgId]) return cfgId;
    return this._sysEntityId(K.sysHomePower);
  }
  _num(stateObj) {
    if (!stateObj) return null;
    const n = Number(stateObj.state);
    return Number.isNaN(n) ? null : n;
  }
  _attrNum(attrs, key) {
    const value = attrs && attrs[key];
    if (value == null || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  _energyKwh(stateObj) {
    const n = this._num(stateObj);
    if (n == null) return null;
    const unit = String((stateObj.attributes || {}).unit_of_measurement || "kWh").toLowerCase();
    return unit === "wh" ? n / 1000 : n;
  }
  /** Sum numeric states for a key across all batteries. */
  _sum(byKey, key) {
    let total = null;
    for (const s of this._statesFor(byKey, key)) {
      const n = this._num(s);
      if (n != null) total = (total || 0) + n;
    }
    return total;
  }
  /** Convert a power state to Watts regardless of W/kW unit. */
  _watts(stateObj) {
    const n = this._num(stateObj);
    if (n == null) return null;
    const u = (stateObj.attributes.unit_of_measurement || "").toLowerCase();
    return u === "kw" ? n * 1000 : n;
  }
  /** Anker AC models expose a calculated PV-looking value, not DC PV input. */
  _isAnkerAcOnlyModel(model) {
    const normalized = String(model || "").trim().toLowerCase();
    return /(?:^|\s)solarbank\s+(?:max|xe)\s+ac(?:$|\s)/.test(normalized);
  }
  /** Only the verified E5000 Anker models may use aggregate PV in the panel. */
  _isAnkerIndependentPvModel(model) {
    const normalized = String(model || "").trim().toLowerCase();
    return normalized.includes("solarbank 4 e5000 pro");
  }
  _allowAggregateSolar(model, device) {
    const manufacturer = String((device && device.manufacturer) || "").toLowerCase();
    const normalized = String(model || "").trim().toLowerCase();
    const isAnker = manufacturer.includes("anker") || normalized.includes("solarbank");
    if (!isAnker) return true;
    if (this._isAnkerAcOnlyModel(model)) return false;
    return this._isAnkerIndependentPvModel(model);
  }
  /** Sum live power (W) of every enabled excluded device. Configuration comes
   *  from the panel payload so disabling a control entity cannot remove a load
   *  from the diagram. A loaded Enabled switch overrides the persisted value,
   *  preserving live toggles. */
  _excludedPowerW() {
    const hass = this._hass;
    let devices = this._panelConfig.excluded_devices;
    // Compatibility fallback for a cached/older panel registration payload.
    if (!Array.isArray(devices)) {
      const domain = this._domain();
      devices = Object.values(hass.entities || {})
        .filter((e) => e.platform === domain && e.translation_key === "excluded_device_enabled")
        .map((e) => hass.states[e.entity_id])
        .filter(Boolean)
        .map((state) => ({
          enabled: state.state === "on",
          power_sensor: (state.attributes || {}).power_sensor,
          included_in_consumption: (state.attributes || {}).included_in_consumption,
        }));
    }
    if (!devices.length) return null;
    let total = null;
    let included = 0; // portion the home sensor already counts (subtract from Home)
    for (const device of devices) {
      let enabled = device.enabled !== false;
      const enabledState = device.enabled_entity
        ? hass.states[device.enabled_entity]
        : null;
      if (enabledState && enabledState.state === "on") enabled = true;
      else if (enabledState && enabledState.state === "off") enabled = false;
      if (!enabled) continue;
      const sid = device.power_sensor;
      if (!sid) continue; // EV-no-telemetry has no power sensor
      const w = this._watts(hass.states[sid]);
      if (w == null) continue;
      total = (total || 0) + w;
      // Only devices the home sensor already includes (included_in_consumption
      // !== false) may be subtracted from the Home node. "Additional" devices
      // are not in the home sensor, so subtracting them would wrongly drive
      // Home toward 0. Subtract the FULL draw: the excluded-devices node shows
      // the device's full demand, so Home must be total − D for the flow to
      // balance. The exclusion % only changes the supply mix (battery covers
      // more, shown as a larger Battery flow) — not the demand-node magnitudes.
      if (device.included_in_consumption !== false) included += w;
    }
    return total == null ? null : { total, included };
  }

  // --- model builder ---------------------------------------------------------
  /** Build the single source-of-truth model (mirrors the prototype `s`/`agg`). */
  _model() {
    const { byKey, byDevice } = this._index();
    const hass = this._hass;

    // Per-battery list (one device per unit, excluding the "system" virtual one).
    const batteries = [];
    for (const [dev, ids] of byDevice) {
      const socObj = ids.map((id) => hass.states[id]).find((s) => {
        const e = hass.entities[s && s.entity_id];
        return e && e.translation_key === K.batterySoc;
      });
      if (!socObj) continue; // not a battery device
      const modelLabel = (socObj.attributes && socObj.attributes.model) || "";
      const socEntity = hass.entities[socObj.entity_id];
      const device = socEntity && hass.devices && hass.devices[socEntity.device_id];
      const allowAggregateSolar = this._allowAggregateSolar(modelLabel, device);
      const get = (key) => {
        const id = ids.find((i) => {
          const e = hass.entities[i];
          return e && e.translation_key === key;
        });
        return id ? hass.states[id] : null;
      };
      const acW = this._watts(get(K.acPower));
      // Off-grid/backup AC output port (+ discharge). Battery can also discharge
      // through it to backup loads that the grid meter never sees.
      const acoW = this._watts(get(K.acOffgridPower));
      // DC-coupled PV: sum this unit's own MPPT inputs (W, >=0) when exposed.
      let mpptW = null;
      for (const mk of MPPT_KEYS) {
        const s = this._watts(get(mk));
        if (s != null) mpptW = (mpptW || 0) + s;
      }
      // Some batteries (Anker Solarbank 4) publish only the aggregate DC PV
      // value. It uses the same canonical translation key as the Venus total,
      // so it can fill the per-battery model without inventing MPPT channels.
      if (allowAggregateSolar && mpptW == null) {
        const aggregateSolarW = this._watts(get(K.solarPower));
        if (aggregateSolarW != null) mpptW = Math.max(0, aggregateSolarW);
      }
      // Derive cell power from both AC ports (- charge / + discharge), negated to
      // the panel's + charge / - discharge convention. On DC-coupled-PV units the
      // PV charges the cells without crossing the AC port, so add this unit's DC PV to
      // recover the true cell power. ac_power is used instead of the battery_power
      // sensor, whose reported value is unreliable.
      // Zendure exposes no ac_power; fall back to its synthesised battery_power
      // sensor (already + charge / - discharge, MPPT-inclusive).
      const battPwrW = this._watts(get(K.batteryPower));
      // Off-grid output only draws from the cells in Backup Mode (grid down);
      // with the grid present it's fed by passthrough, not the battery.
      const invBackup = /backup/i.test(this._sval(get(K.inverterState)) || "");
      const cellAcoW = invBackup ? acoW || 0 : 0;
      const powerW =
        acW != null ? -acW - cellAcoW + (mpptW || 0) : battPwrW;
      batteries.push({
        dev,
        soc: this._num(socObj),
        powerW,
        mpptW,
        stored: this._num(get(K.storedEnergy)),
        capacity: this._num(get(K.batteryTotalEnergy)),
        inverter: (get(K.inverterState) || {}).state || null,
      });
    }

    const nBat = batteries.length;
    const socList = batteries.map((b) => b.soc).filter((v) => v != null);
    const capList = batteries.map((b) => b.capacity).filter((v) => v != null);

    // ----- aggregates (prefer system sensors, else derive from batteries) -----
    const capacity =
      this._num(this._stateFor(byKey, K.sysCapacity)) ??
      (capList.length ? capList.reduce((a, b) => a + b, 0) : null);
    let soc = this._num(this._stateFor(byKey, K.sysSoc));
    if (soc == null && socList.length) {
      // capacity-weighted average when possible, else plain mean
      const wsum = batteries.reduce(
        (a, b) => (b.soc != null && b.capacity ? a + b.soc * b.capacity : a),
        0
      );
      const csum = batteries.reduce(
        (a, b) => (b.soc != null && b.capacity ? a + b.capacity : a),
        0
      );
      soc = csum ? wsum / csum : socList.reduce((a, b) => a + b, 0) / socList.length;
    }
    let stored = this._num(this._stateFor(byKey, K.sysStored));
    if (stored == null) {
      const s = this._sum(byKey, K.storedEnergy);
      stored = s != null ? s : capacity != null && soc != null ? (capacity * soc) / 100 : null;
    }
    const dailyCharge =
      this._num(this._stateFor(byKey, K.sysDailyCharge)) ?? this._sum(byKey, K.dailyCharge) ?? 0;
    const dailyDischarge =
      this._num(this._stateFor(byKey, K.sysDailyDischarge)) ??
      this._sum(byKey, K.dailyDischarge) ??
      0;

    // active / offline counts
    const activeNum = this._num(this._stateFor(byKey, K.activeBatteries));
    const active = activeNum != null ? activeNum : nBat;
    const nrObj = this._stateFor(byKey, K.nonResponsive);
    let offline = 0;
    if (nrObj) {
      const v = String(nrObj.state).trim().toLowerCase();
      if (v && v !== "none" && v !== "0" && v !== "unknown" && v !== "unavailable") {
        const n = Number(nrObj.state);
        offline = Number.isNaN(n) ? v.split(",").filter(Boolean).length : n;
      }
    }

    // ----- flow (kW) -----
    // battery net: prefer per-battery signed sum (+charge/-discharge), else system.
    let battW = null;
    const battSum = batteries.reduce(
      (a, b) => (b.powerW != null ? (a || 0) + b.powerW : a),
      null
    );
    if (battSum != null) battW = battSum;
    else {
      const c = this._num(this._stateFor(byKey, K.sysChargePower));
      const d = this._num(this._stateFor(byKey, K.sysDischargePower));
      if (c != null || d != null) battW = (c || 0) - (d || 0);
    }
    const battery = battW != null ? battW / 1000 : 0;

    // solar: solar_entity is already the complete production figure — the
    // system_solar_power aggregate (external + independent battery-reported DC PV),
    // or the external-only sensor on AC-derived systems. So use it directly; ΣMPPT is only
    // a fallback for before that aggregate sensor is readable, NOT an addition,
    // otherwise the DC-coupled share is double-counted on vA/vD (#407).
    let solarW = null;
    const solarObj = this._panelConfig.solar_entity
      ? hass.states[this._panelConfig.solar_entity]
      : null;
    const explicitSolarW = this._watts(solarObj);
    const mpptTotalW = batteries.reduce(
      (a, b) => (b.mpptW != null ? (a || 0) + b.mpptW : a),
      null
    );
    if (explicitSolarW != null) solarW = explicitSolarW;
    else if (mpptTotalW != null) solarW = mpptTotalW;
    const solar = solarW != null ? Math.max(0, solarW / 1000) : 0;
    const hasSolar = solarW != null;

    // grid from the configured net meter (+import / -export). Negate when the
    // meter is user-inverted so the panel matches the integration's convention.
    const gridObj = this._panelConfig.grid_entity
      ? hass.states[this._panelConfig.grid_entity]
      : null;
    const gridW = this._watts(gridObj);
    const gridSign = this._panelConfig.grid_inverted ? -1 : 1;
    const grid = gridW != null ? (gridW * gridSign) / 1000 : null;

    // home: explicit sensor (resolved dynamically so entity renames are transparent),
    // else derive  home = grid - battery + solar
    const homeObj = hass.states[this._homeEntityId(hass)] || null;
    const homeW = this._watts(homeObj);
    let home;
    if (homeW != null) home = homeW / 1000;
    else if (grid != null) home = Math.max(0, grid - battery + solar);
    else home = 0;

    // excluded devices: summed power of all enabled excluded loads (kW). null
    // when none expose a power sensor — the flow node is hidden in that case.
    const excludedW = this._excludedPowerW();
    const hasExcluded = excludedW != null;
    const excluded = hasExcluded ? excludedW.total / 1000 : null;

    // Subtract from the Home node only the excluded devices the home sensor
    // already counts (included_in_consumption). They are drawn as their own
    // node, so subtracting avoids double-counting. "Additional" devices are not
    // in the home sensor — subtracting them would wrongly drive Home to 0.
    if (hasExcluded) home = Math.max(0, home - excludedW.included / 1000);

    const netBalance = this._num(this._stateFor(byKey, K.netBalance));

    // total available power for the bar (sum of per-unit max limits, else heuristic)
    const maxCh = this._sum(byKey, K.maxChargePower);
    // Marstek exposes max_discharge_power; Zendure exposes inverse_max_power.
    // Each unit has only one of the two, so summing both keys is safe.
    const maxDis =
      (this._sum(byKey, K.maxDischargePower) || 0) +
      (this._sum(byKey, K.inverseMaxPower) || 0) || null;

    // ----- diagnostics -----
    // raw state object per diagnostic row, localized later via formatEntityState
    const diagStates = {};
    for (const row of DIAG_ROWS) diagStates[row.key] = this._stateFor(byKey, row.key);
    const alarmObj = diagStates[K.sysAlarm];

    // exact daily solar/home/grid energy (kWh) from the backend accumulator sensors
    const dailySolar = this._num(this._stateFor(byKey, K.sysDailySolar));
    const dailyHome = this._num(this._stateFor(byKey, K.sysDailyHome));
    const dailyGridImport = this._num(this._stateFor(byKey, K.sysDailyGridImport));
    const dailyGridExport = this._num(this._stateFor(byKey, K.sysDailyGridExport));
    const profileObj = this._stateFor(byKey, K.consumptionProfile);
    const predictiveObj = this._stateFor(byKey, K.predictiveActive);
    const predictiveAttrs = predictiveObj && predictiveObj.attributes ? predictiveObj.attributes : {};
    const forecastInitial = this._attrNum(
      predictiveAttrs, "solar_forecast_initial_kwh"
    );
    const forecastRemaining = this._attrNum(
      predictiveAttrs, "remaining_solar_kwh"
    );
    const remainingEntity = this._panelConfig.solar_forecast_remaining_entity
      ? hass.states[this._panelConfig.solar_forecast_remaining_entity]
      : null;
    const solarRemaining = forecastRemaining ?? this._energyKwh(remainingEntity);
    const expectedConsumption =
      this._num(profileObj) ??
      this._attrNum(predictiveAttrs, "daily_avg_consumption_kwh");

    return {
      nBat,
      solar,
      hasSolar,
      home,
      grid,
      battery,
      excluded,
      hasExcluded,
      soc,
      capacity,
      stored,
      dailyCharge,
      dailyDischarge,
      dailySolar,
      dailyHome,
      dailyGridImport,
      dailyGridExport,
      forecastInitial,
      solarRemaining,
      expectedConsumption,
      active,
      offline,
      netBalance,
      maxCharge: maxCh,
      maxDischarge: maxDis,
      alarm: alarmObj ? alarmObj.state : null,
      diagStates,
    };
  }

  // --- formatting ------------------------------------------------------------
  _nf(n, d = 2) {
    if (n == null || Number.isNaN(n)) return "—";
    return Number(n).toLocaleString(this._lang(), {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }
  _fmtPower(w) {
    if (w == null || Number.isNaN(w)) return { v: "—", u: "" };
    const a = Math.abs(w);
    if (a < 1000) return { v: Math.round(w).toLocaleString(this._lang()), u: "W" };
    return { v: this._nf(w / 1000, 2), u: "kW" };
  }
  _clamp(x, a, b) {
    return Math.max(a, Math.min(b, x));
  }

  // --- update / render -------------------------------------------------------
  _update() {
    if (!this._hass || !this.isConnected) return;
    if (!this._built) {
      this._renderShell();
      this._built = true;
    }
    if (this._view === "resumen") this._patch(this._model());
    else if (this._view === "baterias") this._patchBatteries(this._batteryModel());
    else if (this._view === "control") this._patchControl();
  }

  _renderShell() {
    this.shadowRoot.innerHTML = "";
    this.shadowRoot.appendChild(this._styleEl());

    const app = document.createElement("div");
    app.className = "app";
    app.appendChild(this._renderAppbar());

    const main = document.createElement("div");
    main.className = "main";
    app.appendChild(main);
    this._main = main;

    this.shadowRoot.appendChild(app);
    this._setView(this._view); // builds the active view
  }

  _renderAppbar() {
    const bar = document.createElement("div");
    bar.className = "appbar";

    const brand = document.createElement("div");
    brand.className = "brand";
    brand.innerHTML = `
      <div class="logo">O</div>
      <div class="btext">
        <div class="bt-name">${this._title()}</div>
        <div class="bt-sub">${this._t("subtitle")}</div>
      </div>`;
    brand.querySelector(".logo").addEventListener("click", () =>
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }))
    );

    const tabs = document.createElement("div");
    tabs.className = "tabs";
    const TABS = [
      ["resumen", "mdi:view-dashboard-outline", this._t("tabResumen")],
      ["baterias", "mdi:battery-high", this._t("tabBaterias")],
      ["control", "mdi:tune-variant", this._t("tabControl")],
    ];
    this._tabEls = {};
    for (const [id, icon, label] of TABS) {
      const t = document.createElement("button");
      t.className = "tab";
      t.innerHTML = `<ha-icon icon="${icon}"></ha-icon><span class="tab-label">${label}</span>`;
      t.addEventListener("click", () => this._setView(id));
      this._tabEls[id] = t;
      tabs.appendChild(t);
    }

    bar.appendChild(brand);
    bar.appendChild(tabs);
    return bar;
  }

  _setView(view) {
    this._view = view;
    for (const [id, el] of Object.entries(this._tabEls || {})) {
      el.classList.toggle("active", id === view);
    }
    if (!this._main) return;
    this._detachDailyOperationGlobalListeners();
    this._main.innerHTML = "";
    if (view === "resumen") {
      this._main.appendChild(this._renderResumen());
      this._patch(this._model());
    } else if (view === "baterias") {
      this._main.appendChild(this._renderBaterias());
      this._patchBatteries(this._batteryModel());
    } else if (view === "control") {
      this._main.appendChild(this._renderControl());
      this._patchControl();
    } else {
      this._main.appendChild(this._placeholder(view));
    }
  }

  _placeholder(view) {
    const names = { baterias: this._t("tabBaterias"), control: this._t("tabControl") };
    const d = document.createElement("div");
    d.className = "placeholder";
    d.innerHTML = `
      <ha-icon icon="mdi:hammer-wrench"></ha-icon>
      <h3>${names[view] || view}</h3>
      <p>${this._t("placeholderMsg")}</p>`;
    return d;
  }

  // ===== Resumen view ========================================================
  _renderResumen() {
    this._detachDailyOperationGlobalListeners();
    this._r = {};
    this._edgeSig = {};
    this._buildCards();
    const c = this._cards;
    const wrap = (cls, children) => {
      const d = document.createElement("div");
      d.className = cls;
      children.forEach((ch) => d.appendChild(ch));
      return d;
    };
    // hero (SOC + power + diagnostics) on top; below, Flujo on the left and a
    // 2×2 chart grid on the right (top row auto-fits Energía hoy, bottom fills)
    return wrap("res-stack", [
      c.soc,
      wrap("resumen-lower", [
        c.flow,
        wrap("charts-2x2", [c.daily, c.weekly, c.power, c.mini]),
      ]),
      c.dailyOperation,
    ]);
  }

  _card(title, icon) {
    const card = document.createElement("div");
    card.className = "card";
    const head = document.createElement("div");
    head.className = "card-head";
    head.innerHTML = `<span class="ic"><ha-icon icon="${icon}"></ha-icon></span><h2>${title}</h2>`;
    card.appendChild(head);
    return { card, head };
  }

  _buildCards() {
    this._cards = {
      flow: this._buildFlowCard(),
      soc: this._buildSocCard(),
      daily: this._buildDailyCard(),
      weekly: this._buildWeeklyCard(),
      power: this._buildPowerHistoryCard(),
      mini: this._buildMiniHistory(),
      dailyOperation: this._buildDailyOperationTimelineCard(),
    };
  }

  // ----- Daily operation timeline ------------------------------------------
  /**
   * Build the timeline shell once. The sensor is deliberately optional: the
   * card is hidden until the versioned timeline entity is available, so an
   * older backend cannot break the rest of Resumen.
   */
  _buildDailyOperationTimelineCard() {
    const { card, head } = this._card(this._t("dailyOperationTitle"), "mdi:chart-timeline-variant");
    card.classList.add("daily-operation-card");
    card.hidden = true;
    this._dailyOpPinnedIndex = null;

    const badge = document.createElement("span");
    badge.className = "daily-op-badge";
    badge.innerHTML = `<span class="daily-op-badge-dot"></span><span></span>`;
    head.appendChild(badge);

    const description = document.createElement("div");
    description.className = "daily-op-description muted";
    description.textContent = this._t("dailyOperationDescription");
    card.appendChild(description);

    const toolbar = document.createElement("div");
    toolbar.className = "daily-op-toolbar";
    const legend = document.createElement("div");
    legend.className = "daily-op-legend";
    legend.innerHTML =
      `<span class="daily-op-legend-item"><i class="daily-op-swatch daily-op-swatch-solar-window"></i>${this._t("dailyOperationSolarWindow")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-swatch daily-op-swatch-solar"></i>${this._t("dailyOperationSolarCharge")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-swatch daily-op-swatch-grid"></i>${this._t("dailyOperationGridCharge")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-swatch daily-op-swatch-hourly-balance"></i>${this._t("dailyOperationHourlyBalance")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-swatch daily-op-swatch-discharge"></i>${this._t("dailyOperationDischarge")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-swatch daily-op-swatch-not-needed"></i>${this._t("dailyOperationNotNeeded")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-line daily-op-line-solar"></i>${this._t("dailyOperationSolar")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-line daily-op-line-consumption"></i>${this._t("dailyOperationConsumption")}</span>` +
      `<span class="daily-op-legend-item"><i class="daily-op-line daily-op-line-soc"></i>${this._t("dailyOperationSoc")}</span>`;
    toolbar.appendChild(legend);

    const nav = document.createElement("div");
    nav.className = "daily-op-nav";
    const previous = document.createElement("button");
    previous.type = "button";
    previous.className = "daily-op-nav-btn";
    previous.setAttribute("aria-label", this._t("dailyOperationPrevious"));
    previous.title = this._t("dailyOperationPrevious");
    previous.innerHTML = "<ha-icon icon=\"mdi:chevron-left\"></ha-icon>";
    const next = document.createElement("button");
    next.type = "button";
    next.className = "daily-op-nav-btn";
    next.setAttribute("aria-label", this._t("dailyOperationNext"));
    next.title = this._t("dailyOperationNext");
    next.innerHTML = "<ha-icon icon=\"mdi:chevron-right\"></ha-icon>";
    previous.addEventListener("click", () => this._scrollDailyOperation(-1));
    next.addEventListener("click", () => this._scrollDailyOperation(1));
    nav.append(previous, next);
    toolbar.appendChild(nav);
    card.appendChild(toolbar);

    const notice = document.createElement("div");
    notice.className = "daily-op-notice";
    notice.setAttribute("role", "status");
    notice.hidden = true;
    card.appendChild(notice);

    const layout = document.createElement("div");
    layout.className = "daily-op-layout";
    const yAxis = document.createElement("div");
    yAxis.className = "daily-op-yaxis";
    yAxis.setAttribute("aria-hidden", "true");
    layout.appendChild(yAxis);

    const viewport = document.createElement("div");
    viewport.className = "daily-op-viewport";
    viewport.tabIndex = 0;
    viewport.setAttribute("role", "region");
    viewport.setAttribute("aria-label", this._t("dailyOperationTitle"));
    const stage = document.createElement("div");
    stage.className = "daily-op-stage";

    const uid = `mv-daily-op-${(this._dailyOpUid = (this._dailyOpUid || 0) + 1)}`;

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("daily-op-svg");
    svg.setAttribute("viewBox", `0 0 ${DAILY_OPERATION_TOTAL_INTERVALS * 10} 190`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML =
      `<g class="daily-op-grid-lines">` +
      Array.from({ length: 6 }, (_, i) => `<line x1="0" y1="${32 + i * 26.4}" x2="${DAILY_OPERATION_TOTAL_INTERVALS * 10}" y2="${32 + i * 26.4}"/>`).join("") +
      Array.from({ length: DAILY_OPERATION_TOTAL_HOURS + 1 }, (_, i) => `<line class="daily-op-hour-line" x1="${i * 40}" y1="24" x2="${i * 40}" y2="170"/>`).join("") +
      `</g>` +
      `<path class="daily-op-path daily-op-path-solar-actual" fill="none"></path>` +
      `<path class="daily-op-path daily-op-path-solar-forecast" fill="none"></path>` +
      `<path class="daily-op-path daily-op-path-consumption-actual" fill="none"></path>` +
      `<path class="daily-op-path daily-op-path-consumption-forecast" fill="none"></path>` +
      `<path class="daily-op-path daily-op-path-soc-actual" fill="none"></path>` +
      `<path class="daily-op-path daily-op-path-soc-forecast" fill="none"></path>`;
    stage.appendChild(svg);

    // Keep the current-time marker in HTML so its label has a stable pixel
    // size instead of stretching with the SVG viewBox.
    const nowMarker = document.createElement("div");
    nowMarker.className = "daily-op-now-marker";
    const nowText = document.createElement("span");
    nowText.className = "daily-op-now-text";
    nowMarker.appendChild(nowText);
    stage.appendChild(nowMarker);

    const labels = document.createElement("div");
    labels.className = "daily-op-hour-labels";
    const hours = document.createElement("div");
    hours.className = "daily-op-hours";
    const cells = [];
    for (let hour = 0; hour < DAILY_OPERATION_TOTAL_HOURS; hour++) {
      const dayOffset = Math.floor(hour / 24);
      const clockHour = hour % 24;
      const label = document.createElement("span");
      label.className = "daily-op-hour-label";
      label.textContent = dayOffset ? `${String(clockHour).padStart(2, "0")} +${dayOffset}` : String(clockHour).padStart(2, "0");
      labels.appendChild(label);

      const group = document.createElement("div");
      group.className = "daily-op-hour";
      group.dataset.hour = String(hour);
      for (let quarter = 0; quarter < 4; quarter++) {
        const index = hour * 4 + quarter;
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "daily-op-cell daily-op-base-neutral";
        cell.dataset.index = String(index);
        cell.tabIndex = 0;
        cell.setAttribute("aria-label", this._dailyOperationTimeRange(index));
        const delay = document.createElement("span");
        delay.className = "daily-op-delay-mark";
        delay.hidden = true;
        delay.innerHTML = "<ha-icon icon=\"mdi:clock-outline\"></ha-icon>";
        const setpoint = document.createElement("span");
        setpoint.className = "daily-op-setpoint-mark";
        setpoint.hidden = true;
        setpoint.innerHTML = "<ha-icon icon=\"mdi:target\"></ha-icon>";
        cell.append(delay, setpoint);
        cell.addEventListener("mouseenter", () => this._showDailyOperationTooltip(index, cell));
        cell.addEventListener("mouseleave", () => {
          if (this._dailyOpPinnedIndex == null) this._hideDailyOperationTooltip();
        });
        cell.addEventListener("focus", () => this._showDailyOperationTooltip(index, cell));
        cell.addEventListener("blur", () => {
          if (this._dailyOpPinnedIndex == null) this._hideDailyOperationTooltip();
        });
        cell.addEventListener("click", () => {
          if (this._dailyOpPinnedIndex === index) {
            this._dailyOpPinnedIndex = null;
            this._hideDailyOperationTooltip();
          } else {
            this._dailyOpPinnedIndex = index;
            this._showDailyOperationTooltip(index, cell);
          }
        });
        cell.addEventListener("keydown", (event) => {
          if (event.key === "Escape") {
            this._dailyOpPinnedIndex = null;
            this._hideDailyOperationTooltip();
            cell.blur();
            return;
          }
          const delta = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1
            : event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 0;
          if (delta) {
            event.preventDefault();
            const target = this._r.dailyOperation && this._r.dailyOperation.cells[index + delta];
            if (target) target.focus();
          }
          if (event.key === "Home" || event.key === "End") {
            event.preventDefault();
            const target = this._r.dailyOperation && this._r.dailyOperation.cells[event.key === "Home" ? 0 : DAILY_OPERATION_TOTAL_INTERVALS - 1];
            if (target) target.focus();
          }
        });
        group.appendChild(cell);
        cells.push(cell);
      }
      hours.appendChild(group);
    }
    const grid = document.createElement("div");
    grid.className = "daily-op-grid";
    grid.append(labels, hours);
    stage.appendChild(grid);

    const tooltip = document.createElement("div");
    tooltip.className = "daily-op-tooltip";
    tooltip.id = `${uid}-tooltip`;
    tooltip.setAttribute("role", "tooltip");
    tooltip.setAttribute("aria-live", "polite");
    tooltip.hidden = true;

    viewport.appendChild(stage);
    layout.appendChild(viewport);
    const socAxis = document.createElement("div");
    socAxis.className = "daily-op-soc-axis";
    socAxis.setAttribute("aria-hidden", "true");
    socAxis.innerHTML = `<small>SOC %</small><div class="daily-op-soc-axis-ticks">` +
      [100, 75, 50, 25, 0].map((value) => `<span>${value}</span>`).join("") + `</div>`;
    layout.appendChild(socAxis);
    card.appendChild(layout);
    // The tooltip is positioned against the card, outside the horizontally
    // clipped viewport, so the first and last intervals remain fully visible.
    card.appendChild(tooltip);

    const scrollState = { manual: false, initialized: false, programmaticUntil: 0 };
    viewport.addEventListener("scroll", () => {
      if (Date.now() > scrollState.programmaticUntil) {
        scrollState.manual = true;
        this._dailyOpPinnedIndex = null;
        this._hideDailyOperationTooltip();
      }
      this._updateDailyOperationNav();
      this._scheduleDailyOperationPathRefresh();
    }, { passive: true });
    const markManualScroll = () => {
      if (Date.now() > scrollState.programmaticUntil) scrollState.manual = true;
    };
    viewport.addEventListener("wheel", markManualScroll, { passive: true });
    viewport.addEventListener("touchstart", markManualScroll, { passive: true });
    viewport.addEventListener("pointerdown", markManualScroll, { passive: true });
    this._r.dailyOperation = {
      card, badge, notice, yAxis, socAxis, viewport, stage, svg, tooltip, previous, next,
      cells, labels, hours,
      paths: {
        solarActual: svg.querySelector(".daily-op-path-solar-actual"),
        solarForecast: svg.querySelector(".daily-op-path-solar-forecast"),
        consumptionActual: svg.querySelector(".daily-op-path-consumption-actual"),
        consumptionForecast: svg.querySelector(".daily-op-path-consumption-forecast"),
        socActual: svg.querySelector(".daily-op-path-soc-actual"),
        socForecast: svg.querySelector(".daily-op-path-soc-forecast"),
      },
      nowMarker,
      nowText,
      scrollState,
      localDate: null,
    };
    this._attachDailyOperationGlobalListeners();
    return card;
  }

  _dismissDailyOperationFloatingTooltip() {
    const ref = this._r.dailyOperation;
    // A fixed tooltip is not clipped by the timeline viewport. Dismiss it
    // whenever its anchor can move during page/visual-viewport scrolling.
    if (!ref || ref.tooltip.hidden) return;
    this._dailyOpPinnedIndex = null;
    this._hideDailyOperationTooltip();
  }

  _attachDailyOperationGlobalListeners() {
    if (this._dailyOperationGlobalListenersAttached || !this._r.dailyOperation) return;
    window.addEventListener("scroll", this._onDailyOperationViewportChange, { capture: true, passive: true });
    this._dailyOperationVisualViewport = window.visualViewport || null;
    if (this._dailyOperationVisualViewport) {
      this._dailyOperationVisualViewport.addEventListener("scroll", this._onDailyOperationViewportChange, { passive: true });
      this._dailyOperationVisualViewport.addEventListener("resize", this._onDailyOperationViewportChange, { passive: true });
    }
    this._dailyOperationGlobalListenersAttached = true;
  }

  _detachDailyOperationGlobalListeners() {
    if (!this._dailyOperationGlobalListenersAttached) return;
    window.removeEventListener("scroll", this._onDailyOperationViewportChange, true);
    if (this._dailyOperationVisualViewport) {
      this._dailyOperationVisualViewport.removeEventListener("scroll", this._onDailyOperationViewportChange);
      this._dailyOperationVisualViewport.removeEventListener("resize", this._onDailyOperationViewportChange);
    }
    this._dailyOperationVisualViewport = null;
    this._dailyOperationGlobalListenersAttached = false;
  }

  /**
   * Resolve the timeline through its stable registry identity first.  Entity
   * IDs are user-editable in Home Assistant, while translation_key is not.
   * A config override remains useful on older frontends without registry data.
   */
  _dailyOperationEntityId() {
    const hass = this._hass;
    if (!hass || !hass.states) return null;
    const configured = [
      this._panelConfig.daily_operation_timeline_entity,
      this._panelConfig.daily_operation_entity,
      this._panelConfig.dailyOperationTimelineEntity,
    ].filter(Boolean);
    const domain = this._domain();
    const registered = Object.values(hass.entities || {})
      .filter((entry) => entry && entry.entity_id && entry.entity_id.startsWith("sensor.")
        && (entry.translation_key === "daily_operation_timeline"
          || (entry.platform === domain
            && String(entry.unique_id || "").endsWith("daily_operation_timeline"))))
      .map((entry) => entry.entity_id);
    const candidates = [...configured, ...registered, "sensor.omnibattery_daily_operation_timeline"];
    return candidates.find((id) => hass.states[id]) || null;
  }

  _dailyOperationArray(value) {
    const values = Array.isArray(value) ? value : value && Array.isArray(value.values) ? value.values : null;
    if (!values) return null;
    const count = arguments.length > 1 ? arguments[1] : DAILY_OPERATION_BASE_INTERVALS;
    return Array.from({ length: count }, (_, index) => values[index] == null ? null : values[index]);
  }

  _dailyOperationPick(source, keys) {
    if (!source || typeof source !== "object") return undefined;
    for (const key of keys) {
      if (Object.prototype.hasOwnProperty.call(source, key)) return source[key];
    }
    return undefined;
  }

  _dailyOperationBool(value) {
    if (value === true || value === 1) return true;
    return typeof value === "string" && ["true", "on", "yes", "1"].includes(value.toLowerCase());
  }

  _dailyOperationNumber(value) {
    if (value == null || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  _dailyOperationExtensionItems(data) {
    const raw = data && (
      data.extended_projection
      || data.forecast_extension
      || data.extended_intervals
    );
    return Array.isArray(raw) ? raw.filter((item) => item && typeof item === "object") : [];
  }

  _dailyOperationExtensionIndex(item, fallback) {
    const candidate = this._dailyOperationNumber(
      item && (item.extension_index ?? item.index)
    );
    const index = Math.floor(candidate ?? fallback);
    return Math.max(0, Math.min(DAILY_OPERATION_EXTENSION_INTERVALS - 1, index));
  }

  _dailyOperationExtendedArray(base, extension, keys) {
    const result = Array.from({ length: DAILY_OPERATION_TOTAL_INTERVALS }, (_, index) =>
      Array.isArray(base) && index < DAILY_OPERATION_BASE_INTERVALS ? base[index] : null
    );
    extension.forEach((item, fallback) => {
      const value = this._dailyOperationPick(item, keys);
      if (value === undefined) return;
      const index = DAILY_OPERATION_BASE_INTERVALS + this._dailyOperationExtensionIndex(item, fallback);
      result[index] = value;
    });
    return result;
  }

  _dailyOperationState() {
    const entityId = this._dailyOperationEntityId();
    if (!entityId) return null;
    const stateObj = this._hass.states[entityId];
    const attributes = (stateObj && stateObj.attributes) || {};
    const nested = attributes.timeline || attributes.daily_operation_timeline;
    const data = nested && typeof nested === "object" ? { ...attributes, ...nested } : attributes;
    const unavailable = !stateObj || ["unknown", "unavailable"].includes(String(stateObj.state).toLowerCase());
    const series = data.series && typeof data.series === "object" ? data.series : data;
    const operations = data.operations && typeof data.operations === "object" ? data.operations : data;
    const extension = this._dailyOperationExtensionItems(data);
    const pickSeries = (keys) => this._dailyOperationArray(this._dailyOperationPick(series, keys));
    const pickOperation = (keys) => this._dailyOperationArray(this._dailyOperationPick(operations, keys));
    const actualSolarBase = pickSeries(["solar_actual_kwh", "solar_actual"]);
    const forecastSolarBase = pickSeries(["solar_forecast_kwh", "solar_forecast"]);
    const actualConsumptionBase = pickSeries(["consumption_actual_kwh", "consumption_actual", "home_actual_kwh"]);
    const forecastConsumptionBase = pickSeries(["consumption_forecast_kwh", "consumption_forecast", "home_forecast_kwh"]);
    const storedActualSocBase = pickOperation(["actual_soc_pct"]);
    const forecastSocBase = pickOperation(["planned_soc_pct", "soc_end_pct", "stored_soc_end_pct"]);
    const actualSolar = this._dailyOperationExtendedArray(actualSolarBase, [], []);
    const forecastSolar = this._dailyOperationExtendedArray(
      forecastSolarBase, extension, ["solar_kwh", "solar_forecast_kwh"]
    );
    const actualConsumption = this._dailyOperationExtendedArray(actualConsumptionBase, [], []);
    const forecastConsumption = this._dailyOperationExtendedArray(
      forecastConsumptionBase, extension, ["consumption_kwh", "consumption_forecast_kwh"]
    );
    const storedActualSoc = this._dailyOperationExtendedArray(storedActualSocBase, [], []);
    const forecastSoc = this._dailyOperationExtendedArray(
      forecastSocBase, extension, ["soc_end_pct", "planned_soc_pct", "stored_soc_end_pct"]
    );
    const currentFallback = this._dateParts();
    const fallbackIndex = currentFallback.hour * 4 + Math.floor(currentFallback.minute / 15);
    const currentIndex = Math.max(0, Math.min(DAILY_OPERATION_BASE_INTERVALS - 1,
      Math.floor(this._dailyOperationNumber(data.current_index) ?? fallbackIndex)));
    const currentProgress = this._clamp(
      this._dailyOperationNumber(data.current_progress) ?? (currentFallback.minute % 15) / 15,
      0, 1
    );
    const recorderSoc = data.local_date && data.local_date === this._dailyOperationSocHistoryDate
      ? this._dailyOperationSocHistory : null;
    const actualSoc = Array.from({ length: DAILY_OPERATION_TOTAL_INTERVALS }, (_, index) => {
      const stored = this._dailyOperationValueAt(storedActualSoc, index);
      if (stored != null) return stored;
      return index <= currentIndex ? this._dailyOperationValueAt(recorderSoc, index) : null;
    });
    const rawDecision = this._dailyOperationPick(operations, ["grid_charge_decision"]);
    const actualDecisionBase = this._dailyOperationArray(this._dailyOperationPick(operations, ["actual_grid_charge_decision"]));
    const plannedDecisionBase = this._dailyOperationArray(this._dailyOperationPick(operations, ["planned_grid_charge_decision"]));
    let gridDecisionBase = null;
    if (Array.isArray(rawDecision)) gridDecisionBase = this._dailyOperationArray(rawDecision);
    else if (rawDecision && typeof rawDecision === "object") {
      gridDecisionBase = this._dailyOperationArray(rawDecision.values || rawDecision.planned || rawDecision.actual);
    }
    const actualDecision = this._dailyOperationExtendedArray(actualDecisionBase, [], []);
    const plannedDecision = this._dailyOperationExtendedArray(
      plannedDecisionBase, extension, ["planned_grid_charge_decision", "grid_charge_decision"]
    );
    const gridDecision = this._dailyOperationExtendedArray(
      gridDecisionBase, extension, ["grid_charge_decision", "planned_grid_charge_decision"]
    );
    const flags = this._dailyOperationPick(data, ["dst_flags", "local_time_flags"]);
    const flagArray = Array.from({ length: DAILY_OPERATION_TOTAL_INTERVALS }, (_, index) =>
      Array.isArray(flags) && index < DAILY_OPERATION_BASE_INTERVALS ? flags[index] : null
    );
    const skippedBase = this._dailyOperationArray(this._dailyOperationPick(data, ["dst_skipped"]));
    const repeatedBase = this._dailyOperationArray(this._dailyOperationPick(data, ["dst_repeated"]));
    const skipped = this._dailyOperationExtendedArray(skippedBase, [], []);
    const repeated = this._dailyOperationExtendedArray(repeatedBase, [], []);
    const extensionHorizon = data.extended_horizon && typeof data.extended_horizon === "object"
      ? data.extended_horizon : {};
    const extensionSkipped = Array.isArray(extensionHorizon.dst_skipped)
      ? extensionHorizon.dst_skipped : [];
    const extensionRepeated = Array.isArray(extensionHorizon.dst_repeated)
      ? extensionHorizon.dst_repeated : [];
    for (let index = 0; index < DAILY_OPERATION_EXTENSION_INTERVALS; index++) {
      skipped[DAILY_OPERATION_BASE_INTERVALS + index] = extensionSkipped[index] ?? null;
      repeated[DAILY_OPERATION_BASE_INTERVALS + index] = extensionRepeated[index] ?? null;
    }
    const isSkipped = Array.from({ length: DAILY_OPERATION_TOTAL_INTERVALS }, (_, index) =>
      this._dailyOperationBool(skipped && skipped[index]) || String(flagArray[index] || "").toLowerCase() === "dst_skipped"
    );
    const isRepeated = Array.from({ length: DAILY_OPERATION_TOTAL_INTERVALS }, (_, index) =>
      this._dailyOperationBool(repeated && repeated[index]) || String(flagArray[index] || "").toLowerCase() === "dst_repeated"
    );
    const sourceData = data.sources && typeof data.sources === "object" ? data.sources : {};
    const source = (kind) => {
      const keys = kind === "solar"
        ? ["solar_forecast", "solar", "solar_source"]
        : kind === "consumption"
          ? ["consumption_forecast", "consumption", "consumption_source"]
          : ["operation_plan", "plan", "operation_source"];
      const value = this._dailyOperationPick(sourceData, keys) ?? data[`${kind}_forecast_source`];
      return value == null ? null : String(value);
    };
    const fallbackReason = (kind) => {
      const value = this._dailyOperationPick(sourceData, kind === "solar"
        ? ["solar_fallback_reason", "solar_forecast_fallback_reason"]
        : ["consumption_fallback_reason", "consumption_forecast_fallback_reason"]
      ) ?? data[`${kind}_fallback_reason`];
      return value == null || value === "" ? null : String(value);
    };
    const operationArray = (keys, extensionKeys = keys) =>
      this._dailyOperationExtendedArray(
        pickOperation(keys), extension, extensionKeys
      );
    const actualMask = operationArray(["actual_action_mask"], []);
    const plannedActions = operationArray(
      ["planned_action_mask"], ["planned_action_mask", "action_mask"]
    );
    const actualCoexistence = operationArray(["actual_coexistence_mask"], []);
    const plannedCoexistence = operationArray(
      ["planned_coexistence_mask"], ["planned_coexistence_mask", "coexistence_mask"]
    );
    const actualContext = operationArray(["actual_context_mask"], []);
    const plannedContext = operationArray(
      ["planned_context_mask"], ["planned_context_mask", "context_mask"]
    );
    const actualSource = operationArray(["actual_source", "actual_sources"], []);
    const plannedSource = operationArray(
      ["planned_source", "planned_sources"], ["planned_source", "source"]
    );
    const delayUntil = operationArray(["delay_until", "planned_delay_until"], ["delay_until"]);
    const chargePower = operationArray(
      ["charge_power_w", "actual_charge_power_w", "planned_charge_power_w"],
      ["charge_power_w"]
    );
    const dischargePower = operationArray(
      ["discharge_power_w", "actual_discharge_power_w", "planned_discharge_power_w"],
      ["discharge_power_w"]
    );
    const solarToBattery = operationArray(["solar_to_battery_kwh"], ["solar_to_battery_kwh"]);
    const gridToBattery = operationArray(["grid_to_battery_kwh"], ["grid_to_battery_kwh"]);
    const chargeToBattery = operationArray(
      ["charge_to_battery_kwh"], ["charge_to_battery_kwh"]
    );
    const actualChargeToBattery = operationArray(["actual_charge_to_battery_kwh"], []);
    const plannedChargeToBattery = operationArray(
      ["planned_charge_to_battery_kwh"], ["charge_to_battery_kwh"]
    );
    const dischargeFromBattery = operationArray(
      ["discharge_from_battery_kwh"], ["discharge_from_battery_kwh"]
    );
    const actualDischargeFromBattery = operationArray(["actual_discharge_from_battery_kwh"], []);
    const plannedDischargeFromBattery = operationArray(
      ["planned_discharge_from_battery_kwh", "battery_to_home_kwh"],
      ["discharge_from_battery_kwh", "battery_to_home_kwh"]
    );
    const batteryToHome = operationArray(["battery_to_home_kwh"], ["battery_to_home_kwh"]);
    const storedEnergyEnd = operationArray(
      ["stored_energy_end_kwh"], ["stored_energy_end_kwh"]
    );
    const socEnd = operationArray(
      ["soc_end_pct", "stored_soc_end_pct"], ["soc_end_pct"]
    );
    const realtimeMode = ["real_time", "realtime_price", "realtime"].some((mode) =>
      String(data.mode || "").toLowerCase().replace(/[- ]/g, "_").includes(mode)
    );
    const hasFutureValues = [plannedActions, forecastSolar, forecastConsumption, forecastSoc].some((array) =>
      Array.isArray(array) && array.slice(currentIndex + 1).some((value) =>
        this._dailyOperationNumber(value) != null && this._dailyOperationNumber(value) !== 0
      )
    );
    // Operation masks are structurally emitted as 96 zeroes even if the
    // manager is absent.  A zero-valued energy/SOC sample is still genuine
    // evidence, but zero masks alone must not make an empty DTO look valid.
    const hasSeriesEvidence = [actualSolar, forecastSolar, actualConsumption, forecastConsumption, actualSoc, forecastSoc]
      .some((array) => Array.isArray(array) && array.some((value) => this._dailyOperationNumber(value) != null));
    const hasActionEvidence = [actualMask, plannedActions]
      .some((array) => Array.isArray(array) && array.some((value) => (this._dailyOperationNumber(value) || 0) !== 0));
    const hasValues = data.timeline_available !== false && (hasSeriesEvidence || hasActionEvidence);
    return {
      entityId, stateObj, data, unavailable, hasValues,
      localDate: data.local_date || null,
      timezone: data.timezone || null,
      generatedAt: data.generated_at || null,
      planEvaluatedAt: data.plan_evaluated_at || null,
      setpointInfo: data.setpoint && typeof data.setpoint === "object" ? data.setpoint : null,
      delayInfo: data.delay && typeof data.delay === "object" ? data.delay : null,
      currentIndex, currentProgress,
      mode: data.mode == null ? null : String(data.mode),
      stale: this._dailyOperationBool(data.stale),
      staleReason: data.stale_reason == null ? null : String(data.stale_reason),
      actualSolar, forecastSolar, actualConsumption, forecastConsumption, actualSoc, forecastSoc,
      actualMask,
      plannedMask: plannedActions,
      actualCoexistence,
      plannedCoexistence,
      actualContext,
      plannedContext,
      actualSource,
      plannedSource,
      gridDecision, actualDecision, plannedDecision,
      delayUntil,
      chargePower,
      dischargePower,
      coverage: pickSeries(["actual_coverage_s", "coverage_s"]),
      solarCoverage: pickSeries(["solar_actual_coverage_s", "actual_coverage_s", "coverage_s"]),
      consumptionCoverage: pickSeries(["consumption_actual_coverage_s", "actual_coverage_s", "coverage_s"]),
      solarToBattery,
      gridToBattery,
      chargeToBattery,
      actualChargeToBattery,
      plannedChargeToBattery,
      dischargeFromBattery,
      actualDischargeFromBattery,
      plannedDischargeFromBattery,
      batteryToHome,
      storedEnergyEnd,
      socEnd,
      observedSeconds: this._dailyOperationPick(operations, [
        "observed_seconds_by_action_by_interval",
        "observed_seconds_by_action",
      ]),
      isSkipped, isRepeated, source, fallbackReason,
      realtimeNoFuture: realtimeMode && !hasFutureValues,
    };
  }

  _dailyOperationMaskAt(array, index) {
    if (!Array.isArray(array) || array[index] == null) return null;
    const value = this._dailyOperationNumber(array[index]);
    return value == null ? null : value | 0;
  }

  _dailyOperationValueAt(array, index) {
    return Array.isArray(array) ? this._dailyOperationNumber(array[index]) : null;
  }

  _dailyOperationChoiceAt(array, index) {
    if (!Array.isArray(array) || array[index] == null || array[index] === "") return null;
    return String(array[index]).toLowerCase();
  }

  _dailyOperationActionBits(mask) {
    const value = Number(mask) || 0;
    return [1, 2, 4].filter((bit) => (value & bit) !== 0);
  }

  _dailyOperationActionKey(bit) {
    return bit === 1 ? "solar" : bit === 2 ? "grid" : "discharge";
  }

  _dailyOperationActionLabel(bit) {
    return this._t(bit === 1 ? "dailyOperationSolarCharge" : bit === 2 ? "dailyOperationGridCharge" : "dailyOperationDischarge");
  }

  _dailyOperationStatus(snapshot, index) {
    if (index < snapshot.currentIndex) return "real";
    if (index === snapshot.currentIndex) return "current";
    return "forecast";
  }

  _dailyOperationStatusLabel(status) {
    return this._t(status === "real" ? "dailyOperationReal" : status === "current" ? "dailyOperationCurrent" : "dailyOperationForecast");
  }

  _dailyOperationTimeRange(index) {
    const start = index * 15;
    const end = start + 15;
    const clock = (minutes) => {
      const absoluteHour = Math.floor(minutes / 60);
      const dayOffset = Math.floor(absoluteHour / 24);
      const hour = absoluteHour % 24;
      const suffix = dayOffset ? ` (+${dayOffset})` : "";
      return `${String(hour).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}${suffix}`;
    };
    return `${clock(start)}–${clock(end)}`;
  }

  _dailyOperationSourceLabel(value) {
    if (!value) return null;
    const normalized = String(value).toLowerCase();
    const sourceKeys = {
      provider: "dailyOperationProvider",
      profile: "dailyOperationConsumptionProfile",
      legacy_daily: "dailyOperationLegacyDaily",
      dynamic_schedule: "dailyOperationDynamicSchedule",
      time_slot: "dailyOperationTimeSlot",
      profile_projection: "dailyOperationProfileProjection",
      unavailable: "dailyOperationUnavailable",
      zero_fallback: "dailyOperationZeroFallback",
      fallback: "dailyOperationGenericFallback",
    };
    if (sourceKeys[normalized]) return this._t(sourceKeys[normalized]);
    if (normalized.includes("learned")) return this._t("dailyOperationLearned");
    if (normalized.includes("sinus")) return this._t("dailyOperationSinusoidal");
    return String(value).replace(/_/g, " ");
  }

  _dailyOperationSourceIsObserved(value) {
    if (!value) return true; // Preserve the legacy contract when no per-cell source exists.
    return !/(?:^|[_ -])(command|unavailable|unknown|error)(?:$|[_ -])/i.test(String(value));
  }

  _dailyOperationReasonDict() {
    return DAILY_OPERATION_REASON_I18N[this._lang2()] || DAILY_OPERATION_REASON_I18N.en;
  }

  _dailyOperationFallbackReasonLabel(value) {
    const dictionary = this._dailyOperationReasonDict();
    const labels = String(value || "").split(";").map((part) => part.trim()).filter(Boolean)
      .filter((part) => part.toLowerCase() !== "zero_budget")
      .map((part) => {
        const normalized = part.toLowerCase();
        let key = DAILY_OPERATION_REASON_KEYS[normalized];
        if (!key && normalized.endsWith("_normalization")) key = "normalizationFailed";
        else if (!key && normalized.startsWith("load:")) key = "loadFailed";
        else if (!key && normalized.startsWith("save:")) key = "saveFailed";
        else if (!key && normalized.startsWith("backfill:")) key = "historyUnavailable";
        else if (!key && normalized.startsWith("profile invalidated")) key = "profileReset";
        return dictionary[key || "fallbackUnavailable"];
      });
    return [...new Set(labels)].join(" · ");
  }

  _dailyOperationStaleReasonLabel(value) {
    if (!value) return "";
    const normalized = String(value).trim().toLowerCase();
    const dictionary = this._dailyOperationReasonDict();
    if (normalized === "projection_unavailable") return dictionary.projectionUnavailable;
    if (normalized.startsWith("projection:")) return dictionary.projectionFailed;
    if (normalized.startsWith("runtime:")) return dictionary.runtimeFailed;
    return dictionary.updateFailed;
  }

  _dailyOperationItem(snapshot, index) {
    const status = this._dailyOperationStatus(snapshot, index);
    const actualMask = this._dailyOperationMaskAt(snapshot.actualMask, index);
    const plannedMask = this._dailyOperationMaskAt(snapshot.plannedMask, index);
    let mask = status === "forecast" ? plannedMask : actualMask;
    if (status === "current") {
      // The tooltip labels this interval as observed, so never merge actions
      // projected for its remaining minutes into the observed mask.
      mask = actualMask || 0;
    }
    if (snapshot.isSkipped[index]) mask = 0;
    const contextActual = this._dailyOperationMaskAt(snapshot.actualContext, index);
    const contextPlanned = this._dailyOperationMaskAt(snapshot.plannedContext, index);
    let context = status === "forecast" ? contextPlanned : contextActual;
    if (status === "current") context = contextActual || 0;
    const decision = this._dailyOperationChoiceAt(snapshot.gridDecision, index) ||
      (status === "forecast" ? this._dailyOperationChoiceAt(snapshot.plannedDecision, index) : this._dailyOperationChoiceAt(snapshot.actualDecision, index));
    const operationSource = this._dailyOperationChoiceAt(
      status === "forecast" ? snapshot.plannedSource : snapshot.actualSource,
      index
    );
    const actionSeconds = {};
    if (snapshot.observedSeconds && typeof snapshot.observedSeconds === "object") {
      for (const bit of [1, 2, 4]) {
        const key = this._dailyOperationActionKey(bit);
        if (Array.isArray(snapshot.observedSeconds)) {
          const cellDurations = snapshot.observedSeconds[index] || {};
          const canonical = key === "solar" ? "solar_charge" : key === "grid" ? "grid_charge" : "discharge";
          actionSeconds[key] = this._dailyOperationNumber(
            cellDurations[key] ?? cellDurations[canonical] ?? cellDurations[String(bit)]
          );
        } else {
          const source = snapshot.observedSeconds[key]
            || snapshot.observedSeconds[`${key}_charge`]
            || snapshot.observedSeconds[String(bit)];
          actionSeconds[key] = this._dailyOperationValueAt(source, index);
        }
      }
    }
    const coexistence = status === "forecast"
      ? this._dailyOperationMaskAt(snapshot.plannedCoexistence, index)
      : this._dailyOperationMaskAt(snapshot.actualCoexistence, index);
    if (status !== "forecast" && this._dailyOperationActionBits(mask).length > 1
      && this._dailyOperationActionBits((coexistence || 0) & mask).length < 2) {
      // A quarter-hour mask is a union of every transition. If those actions
      // were sequential, render the one observed for longest instead of
      // implying that contradictory flows happened at the same time.
      const dominant = this._dailyOperationActionBits(mask).reduce((best, bit) => {
        const key = this._dailyOperationActionKey(bit);
        const bestKey = this._dailyOperationActionKey(best);
        return (actionSeconds[key] || 0) > (actionSeconds[bestKey] || 0) ? bit : best;
      });
      mask = dominant;
    }
    const actions = this._dailyOperationActionBits(mask).map((bit) => this._dailyOperationActionKey(bit));
    const solar = status === "forecast"
      ? this._dailyOperationValueAt(snapshot.forecastSolar, index)
      : this._dailyOperationValueAt(snapshot.actualSolar, index);
    const consumption = status === "forecast"
      ? this._dailyOperationValueAt(snapshot.forecastConsumption, index)
      : this._dailyOperationValueAt(snapshot.actualConsumption, index);
    const projectedSolarChargePending = status === "current"
      && ((plannedMask || 0) & 1) !== 0
      && ((actualMask || 0) & 1) === 0;
    const solarSurplus = solar != null && consumption != null && solar > consumption + 0.000001;
    const solarOpportunity = !snapshot.isSkipped[index]
      && ((mask || 0) & 1) === 0
      && (projectedSolarChargePending || solarSurplus);
    const delayState = snapshot.delayInfo && String(snapshot.delayInfo.state || snapshot.delayInfo.status || "").toLowerCase();
    // Missing means an older payload, which remains compatible. An explicit
    // false is authoritative and suppresses stale stored boundaries/statuses.
    const delayEnabled = !snapshot.delayInfo || snapshot.delayInfo.enabled == null
      || this._dailyOperationBool(snapshot.delayInfo.enabled);
    const weeklyDelayBypassed = Boolean(snapshot.delayInfo && (
      snapshot.delayInfo.weekly_full_charge_bypasses_delay === true
      || delayState.trim() === "skipped - full charge day"
    ));
    const topLevelDelay = index === snapshot.currentIndex && (
      delayState.startsWith("delayed") || [
        "waiting for solar", "waiting for forecast", "waiting_for_solar", "waiting", "blocked",
      ].includes(delayState)
    );
    const delayUntil = weeklyDelayBypassed
      ? null
      : ((Array.isArray(snapshot.delayUntil) ? snapshot.delayUntil[index] : null) ??
        (topLevelDelay && snapshot.delayInfo ? snapshot.delayInfo.estimated_unlock_time || snapshot.delayInfo.unlock_time : null));
    // Older payloads could stamp the delay context on every cell merely because
    // the feature was enabled. Historical/future clocks need a real boundary;
    // boundary-less waiting states are meaningful only for the current cell.
    const delay = delayEnabled && !weeklyDelayBypassed
      && ((delayUntil != null && delayUntil !== "") || topLevelDelay);
    // Delay blocks energy entering the battery, not the underlying solar
    // surplus. Keep that opportunity yellow and overlay the clock context;
    // the absence of a solar action bit keeps the interval out of green.
    const solarWindow = solarOpportunity;
    const setpointState = snapshot.setpointInfo && String(snapshot.setpointInfo.state || snapshot.setpointInfo.status || "").toLowerCase();
    const topLevelSetpoint = index === snapshot.currentIndex && ["charging_to_setpoint", "to_setpoint", "charging"].includes(setpointState);
    return {
      index, status, statusLabel: this._dailyOperationStatusLabel(status),
      timeRange: this._dailyOperationTimeRange(index),
      mask: mask || 0, actions, solarWindow, context: context || 0,
      hourlyBalance: ((context || 0) & DAILY_OPERATION_CONTEXT_HOURLY_BALANCE) !== 0,
      decision,
      operationSource,
      observationTrusted: status === "forecast" || this._dailyOperationSourceIsObserved(operationSource),
      delay, delayUntil: delayUntil == null ? null : String(delayUntil),
      setpoint: !weeklyDelayBypassed && (((context || 0) & 1) !== 0 || topLevelSetpoint),
      skipped: snapshot.isSkipped[index], repeated: snapshot.isRepeated[index],
      solarActual: this._dailyOperationValueAt(snapshot.actualSolar, index),
      solarForecast: this._dailyOperationValueAt(snapshot.forecastSolar, index),
      consumptionActual: this._dailyOperationValueAt(snapshot.actualConsumption, index),
      consumptionForecast: this._dailyOperationValueAt(snapshot.forecastConsumption, index),
      socActual: this._dailyOperationValueAt(snapshot.actualSoc, index),
      socForecast: this._dailyOperationValueAt(snapshot.forecastSoc, index),
      coverage: this._dailyOperationValueAt(snapshot.coverage, index),
      chargePower: this._dailyOperationValueAt(snapshot.chargePower, index),
      dischargePower: this._dailyOperationValueAt(snapshot.dischargePower, index),
      solarToBattery: this._dailyOperationValueAt(snapshot.solarToBattery, index),
      gridToBattery: this._dailyOperationValueAt(snapshot.gridToBattery, index),
      chargeToBattery: this._dailyOperationValueAt(
        status === "forecast" ? snapshot.plannedChargeToBattery : snapshot.actualChargeToBattery,
        index
      ) ?? this._dailyOperationValueAt(snapshot.chargeToBattery, index),
      dischargeFromBattery: this._dailyOperationValueAt(
        status === "forecast" ? snapshot.plannedDischargeFromBattery : snapshot.actualDischargeFromBattery,
        index
      ) ?? this._dailyOperationValueAt(snapshot.dischargeFromBattery, index),
      batteryToHome: this._dailyOperationValueAt(snapshot.batteryToHome, index),
      storedEnergyEnd: this._dailyOperationValueAt(snapshot.storedEnergyEnd, index),
      socEnd: this._dailyOperationValueAt(snapshot.socEnd, index),
      actionSeconds,
    };
  }

  _dailyOperationEscape(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
    }[character]));
  }

  _dailyOperationFormatKwh(value) {
    return value == null ? "—" : `${this._nf(value, 3)} kWh`;
  }

  _dailyOperationFormatPower(value) {
    return value == null ? "—" : `${this._nf(value, 0)} W`;
  }

  _dailyOperationDecisionLabel(decision) {
    if (!decision) return null;
    if (decision === "not_needed") return this._t("dailyOperationNotNeeded");
    if (decision === "not_applicable") return this._t("dailyOperationNoAction");
    if (decision === "unknown") return this._t("dailyOperationUnknown");
    return decision.replace(/_/g, " ");
  }

  _dailyOperationAriaLabel(snapshot, item) {
    const parts = [item.timeRange, item.statusLabel];
    if (item.skipped) parts.push(this._t("dailyOperationDSTSkipped"));
    if (item.repeated) parts.push(this._t("dailyOperationDSTRepeated"));
    if (item.solarActual != null) parts.push(`${this._t("dailyOperationSolar")} (${this._t("dailyOperationReal")}): ${this._dailyOperationFormatKwh(item.solarActual)}`);
    if (item.solarForecast != null) parts.push(`${this._t("dailyOperationSolar")} (${this._t("dailyOperationForecast")}): ${this._dailyOperationFormatKwh(item.solarForecast)}`);
    if (item.consumptionActual != null) parts.push(`${this._t("dailyOperationConsumption")} (${this._t("dailyOperationReal")}): ${this._dailyOperationFormatKwh(item.consumptionActual)}`);
    if (item.consumptionForecast != null) parts.push(`${this._t("dailyOperationConsumption")} (${this._t("dailyOperationForecast")}): ${this._dailyOperationFormatKwh(item.consumptionForecast)}`);
    if (item.socActual != null) parts.push(`${this._t("dailyOperationSoc")} (${this._t("dailyOperationReal")}): ${this._nf(item.socActual, 1)} %`);
    if (item.socForecast != null) parts.push(`${this._t("dailyOperationSoc")} (${this._t("dailyOperationForecast")}): ${this._nf(item.socForecast, 1)} %`);
    if (item.coverage != null) parts.push(`${this._t("dailyOperationCoverage")}: ${Math.round(item.coverage)} s`);
    if (item.actions.length) {
      const actionKind = item.status === "forecast"
        ? this._t("dailyOperationPlan")
        : item.observationTrusted ? this._t("dailyOperationObserved") : this._t("dailyOperationSource");
      parts.push(`${actionKind}: ${item.actions.map((key) => this._dailyOperationActionLabel(key === "solar" ? 1 : key === "grid" ? 2 : 4)).join(", ")}`);
    }
    else if (item.solarWindow) parts.push(this._t("dailyOperationSolarWindow"));
    else if (item.decision === "not_needed") parts.push(this._t("dailyOperationNotNeeded"));
    else parts.push(this._t("dailyOperationNoAction"));
    if (!item.observationTrusted && item.operationSource) {
      parts.push(`${this._t("dailyOperationSource")}: ${this._dailyOperationSourceLabel(item.operationSource)}`);
    }
    if (item.hourlyBalance) {
      parts.push(`${this._t("dailyOperationCause")}: ${this._t("dailyOperationHourlyBalance")}`);
    }
    if (item.chargePower != null) parts.push(`${this._t("dailyOperationGridCharge")}: ${this._dailyOperationFormatPower(item.chargePower)}`);
    if (item.dischargePower != null) parts.push(`${this._t("dailyOperationDischarge")}: ${this._dailyOperationFormatPower(item.dischargePower)}`);
    if (item.solarToBattery != null) parts.push(`${this._t("dailyOperationSolarCharge")}: ${this._dailyOperationFormatKwh(item.solarToBattery)}`);
    if (item.gridToBattery != null) parts.push(`${this._t("dailyOperationGridCharge")}: ${this._dailyOperationFormatKwh(item.gridToBattery)}`);
    if (item.chargeToBattery != null && item.actions.some((action) => action === "solar" || action === "grid")) {
      parts.push(`${this._t("dailyOperationEnergyToBattery")}: ${this._dailyOperationFormatKwh(item.chargeToBattery)}`);
    }
    if (item.dischargeFromBattery != null && item.actions.includes("discharge")) {
      parts.push(`${this._t("dailyOperationEnergyFromBattery")}: ${this._dailyOperationFormatKwh(item.dischargeFromBattery)}`);
    }
    if (item.batteryToHome != null && item.dischargeFromBattery == null) {
      parts.push(`${this._t("dailyOperationDischarge")}: ${this._dailyOperationFormatKwh(item.batteryToHome)}`);
    }
    for (const action of item.actions) {
      if (item.actionSeconds[action] != null) parts.push(`${this._t("dailyOperationObserved")} · ${this._dailyOperationActionLabel(action === "solar" ? 1 : action === "grid" ? 2 : 4)}: ${Math.round(item.actionSeconds[action])} s`);
    }
    if (item.decision) parts.push(`${this._t("dailyOperationMode")}: ${this._dailyOperationDecisionLabel(item.decision)}`);
    if (item.setpoint) parts.push(this._t("dailyOperationSetpoint"));
    if (item.delay) {
      parts.push(item.delayUntil ? this._t("dailyOperationUnlock", { time: item.delayUntil }) : this._t("dailyOperationDelay"));
    }
    if (item.socEnd != null) parts.push(`SOC: ${this._nf(item.socEnd, 1)} %`);
    const solarSource = this._dailyOperationSourceLabel(snapshot.source("solar"));
    const consumptionSource = this._dailyOperationSourceLabel(snapshot.source("consumption"));
    const planSource = this._dailyOperationSourceLabel(snapshot.source("plan"));
    if (solarSource) parts.push(`${this._t("dailyOperationSolar")} · ${this._t("dailyOperationSource")}: ${solarSource}`);
    if (consumptionSource) parts.push(`${this._t("dailyOperationConsumption")} · ${this._t("dailyOperationSource")}: ${consumptionSource}`);
    if (planSource) parts.push(`${this._t("dailyOperationPlan")} · ${this._t("dailyOperationSource")}: ${planSource}`);
    if (snapshot.planEvaluatedAt) parts.push(`${this._t("dailyOperationPlan")}: ${snapshot.planEvaluatedAt}`);
    if (snapshot.generatedAt) parts.push(`${this._t("dailyOperationSource")}: ${snapshot.generatedAt}`);
    if (snapshot.stale) {
      const staleReason = this._dailyOperationStaleReasonLabel(snapshot.staleReason);
      parts.push(staleReason ? `${this._t("dailyOperationStale")}: ${staleReason}` : this._t("dailyOperationStale"));
    }
    return parts.join(". ");
  }

  _dailyOperationTooltipHTML(snapshot, item) {
    const escape = (value) => this._dailyOperationEscape(value);
    const row = (label, value) => `<div class="daily-op-tip-row"><span>${escape(label)}</span><strong>${escape(value)}</strong></div>`;
    const rows = [];
    const useForecast = item.status === "forecast";
    const solar = useForecast ? item.solarForecast : item.solarActual;
    const consumption = useForecast ? item.consumptionForecast : item.consumptionActual;
    const soc = useForecast ? item.socForecast : item.socActual;
    if (solar != null) rows.push(row(this._t("dailyOperationSolar"), this._dailyOperationFormatKwh(solar)));
    if (consumption != null) rows.push(row(this._t("dailyOperationConsumption"), this._dailyOperationFormatKwh(consumption)));
    if (soc != null) rows.push(row(this._t("dailyOperationSoc"), `${this._nf(soc, 1)} %`));
    const actionLabels = item.actions.length
      ? item.actions.map((key) => this._dailyOperationActionLabel(key === "solar" ? 1 : key === "grid" ? 2 : 4)).join(", ")
      : item.solarWindow ? this._t("dailyOperationSolarWindow")
        : item.decision === "not_needed" ? this._t("dailyOperationNotNeeded") : this._t("dailyOperationNoAction");
    const actionKind = useForecast
      ? this._t("dailyOperationPlan")
      : item.observationTrusted ? this._t("dailyOperationObserved") : this._t("dailyOperationSource");
    rows.push(row(actionKind, actionLabels));
    if (!item.observationTrusted && item.operationSource) {
      rows.push(row(this._t("dailyOperationSource"), this._dailyOperationSourceLabel(item.operationSource)));
    }
    if (item.hourlyBalance) {
      rows.push(row(this._t("dailyOperationCause"), this._t("dailyOperationHourlyBalance")));
    }
    if (item.chargeToBattery != null && item.actions.some((action) => action === "solar" || action === "grid")) {
      rows.push(row(this._t("dailyOperationEnergyToBattery"), this._dailyOperationFormatKwh(item.chargeToBattery)));
    }
    if (item.dischargeFromBattery != null && item.actions.includes("discharge")) {
      rows.push(row(this._t("dailyOperationEnergyFromBattery"), this._dailyOperationFormatKwh(item.dischargeFromBattery)));
    }
    if (item.setpoint) rows.push(row(this._t("dailyOperationMode"), this._t("dailyOperationSetpoint")));
    if (item.delay) rows.push(row(this._t("dailyOperationDelay"), item.delayUntil ? this._t("dailyOperationUnlock", { time: item.delayUntil }) : this._t("dailyOperationDelay")));
    if (item.repeated) rows.push(row(this._t("dailyOperationDSTRepeated"), this._t("dailyOperationDSTRepeated")));
    return `<div class="daily-op-tip-head"><strong>${escape(item.timeRange)}</strong><span>${escape(item.statusLabel)}</span></div>` + rows.join("");
  }

  _showDailyOperationTooltip(index, cell) {
    const ref = this._r.dailyOperation;
    const snapshot = this._dailyOperationSnapshot;
    if (!ref || !snapshot || snapshot.unavailable) return;
    const item = this._dailyOperationItem(snapshot, index);
    if (this._dailyOpTooltipCell && this._dailyOpTooltipCell !== cell) {
      this._dailyOpTooltipCell.removeAttribute("aria-describedby");
    }
    ref.tooltip.innerHTML = this._dailyOperationTooltipHTML(snapshot, item);
    ref.tooltip.hidden = false;
    ref.tooltip.setAttribute("aria-label", this._dailyOperationAriaLabel(snapshot, item));
    cell.setAttribute("aria-describedby", ref.tooltip.id);
    this._dailyOpTooltipCell = cell;
    const cellRect = cell.getBoundingClientRect();
    const tipWidth = ref.tooltip.offsetWidth || 250;
    const tipHeight = ref.tooltip.offsetHeight || 100;
    const visualViewport = window.visualViewport;
    const viewportLeft = visualViewport ? visualViewport.offsetLeft : 0;
    const viewportTop = visualViewport ? visualViewport.offsetTop : 0;
    const viewportRight = viewportLeft + (visualViewport ? visualViewport.width : window.innerWidth);
    const viewportBottom = viewportTop + (visualViewport ? visualViewport.height : window.innerHeight);
    let left = cellRect.left + cellRect.width / 2 - tipWidth / 2;
    const above = cellRect.top - tipHeight - 8;
    const below = cellRect.bottom + 8;
    let top = above >= viewportTop + 8 ? above
      : below + tipHeight <= viewportBottom - 8 ? below
        : this._clamp(above, viewportTop + 8, Math.max(viewportTop + 8, viewportBottom - tipHeight - 8));
    left = this._clamp(left, viewportLeft + 8, Math.max(viewportLeft + 8, viewportRight - tipWidth - 8));
    ref.tooltip.style.left = `${left}px`;
    ref.tooltip.style.top = `${top}px`;
  }

  _hideDailyOperationTooltip() {
    const ref = this._r.dailyOperation;
    if (!ref) return;
    ref.tooltip.hidden = true;
    ref.tooltip.removeAttribute("aria-label");
    if (this._dailyOpTooltipCell) {
      this._dailyOpTooltipCell.removeAttribute("aria-describedby");
      this._dailyOpTooltipCell = null;
    }
  }

  _scrollDailyOperation(direction) {
    const ref = this._r.dailyOperation;
    if (!ref) return;
    ref.scrollState.manual = true;
    const amount = Math.max(160, ref.viewport.clientWidth * 0.72);
    if (typeof ref.viewport.scrollBy === "function") {
      ref.viewport.scrollBy({ left: direction * amount, behavior: "smooth" });
    } else {
      ref.viewport.scrollLeft += direction * amount;
    }
  }

  _centerDailyOperationNow(snapshot) {
    const ref = this._r.dailyOperation;
    if (!ref || ref.scrollState.initialized || ref.scrollState.manual) return;
    const center = () => {
      if (ref.scrollState.initialized || ref.scrollState.manual) return;
      if (!ref.viewport.clientWidth) return;
      const hourWidth = ref.stage.scrollWidth / DAILY_OPERATION_TOTAL_HOURS;
      const target = snapshot.currentIndex / 4 * hourWidth - (ref.viewport.clientWidth - hourWidth) / 2;
      // Keep the optional post-midnight extension outside the initial view.
      // The existing navigation buttons are the deliberate way to reveal it.
      const baseEnd = ref.stage.scrollWidth * DAILY_OPERATION_BASE_INTERVALS / DAILY_OPERATION_TOTAL_INTERVALS;
      const baseMax = Math.max(0, baseEnd - ref.viewport.clientWidth);
      ref.scrollState.programmaticUntil = Date.now() + 250;
      ref.viewport.scrollLeft = Math.max(0, Math.min(baseMax, target));
      ref.scrollState.initialized = true;
      this._updateDailyOperationNav();
    };
    if (ref.viewport.clientWidth) center();
    else if (typeof requestAnimationFrame === "function") requestAnimationFrame(center);
  }

  _updateDailyOperationNav() {
    const ref = this._r.dailyOperation;
    if (!ref) return;
    const max = Math.max(0, ref.viewport.scrollWidth - ref.viewport.clientWidth);
    ref.previous.disabled = ref.viewport.scrollLeft <= 2;
    ref.next.disabled = ref.viewport.scrollLeft >= max - 2;
  }

  _dailyOperationPlotValues(values, coverage, snapshot, future) {
    if (!Array.isArray(values)) return values;
    const plotted = values.slice();
    if (future) return plotted;
    const index = snapshot.currentIndex;
    const value = this._dailyOperationNumber(plotted[index]);
    const seconds = this._dailyOperationValueAt(coverage, index);
    // The live capture contains energy accumulated so far. Extrapolate only
    // the open cell so it remains comparable with the completed 15-minute
    // cells and does not create a false drop at the now marker.
    if (value != null && seconds != null && seconds >= 60 && seconds < 900) {
      plotted[index] = value * 900 / seconds;
    }
    return plotted;
  }

  _dailyOperationForecastPlotValues(values, actual, snapshot) {
    if (!Array.isArray(values)) return values;
    const plotted = values.slice();
    const index = snapshot.currentIndex;
    const observed = this._dailyOperationValueAt(actual, index);
    // The projection for the open quarter contains only its remaining energy,
    // while the observed series is plotted as a complete quarter. Do not put
    // those different quantities at the same "now" coordinate: use the
    // observed point as the visual hand-off and keep forecast data unchanged
    // from the next interval onward.
    if (observed != null && !snapshot.isSkipped[index]) plotted[index] = observed;
    return plotted;
  }

  _dailyOperationSolarForecastPlotValues(values, actual, snapshot) {
    const plotted = this._dailyOperationForecastPlotValues(values, actual, snapshot);
    if (!Array.isArray(plotted) || !Array.isArray(actual)) return plotted;

    const index = snapshot.currentIndex;
    const coverageAt = this._dailyOperationValueAt(snapshot.solarCoverage, index);
    const currentSolar = this._dailyOperationValueAt(actual, index);
    // A provider curve can remain optimistic after the live PV capture has
    // already reached zero. Require one covered zero interval and prior
    // production so an unobserved cell does not erase a genuine forecast.
    if (currentSolar == null || currentSolar > 0.000001 || coverageAt == null || coverageAt < 60) {
      return plotted;
    }
    let zeroIntervals = 0;
    for (let cursor = index; cursor >= 0; cursor--) {
      const sample = this._dailyOperationValueAt(actual, cursor);
      const coverage = this._dailyOperationValueAt(snapshot.solarCoverage, cursor);
      if (sample == null || coverage == null || coverage < 60 || sample > 0.000001) break;
      zeroIntervals++;
    }
    const priorProduction = actual
      .slice(0, index)
      .some((value) => (this._dailyOperationNumber(value) || 0) > 0.000001);
    if (zeroIntervals < 1 || !priorProduction) return plotted;

    // Keep the optional next-day extension intact; only today's impossible
    // post-sunset forecast is removed from the graph.
    for (let cursor = index + 1; cursor < DAILY_OPERATION_BASE_INTERVALS; cursor++) {
      plotted[cursor] = null;
    }
    return plotted;
  }

  _dailyOperationVisibleRange() {
    const ref = this._r.dailyOperation;
    if (!ref || !ref.stage.scrollWidth || !ref.viewport.clientWidth) {
      return [0, DAILY_OPERATION_BASE_INTERVALS - 1];
    }
    const scale = DAILY_OPERATION_TOTAL_INTERVALS / ref.stage.scrollWidth;
    const start = Math.max(0, Math.floor(ref.viewport.scrollLeft * scale) - 1);
    const end = Math.min(
      DAILY_OPERATION_TOTAL_INTERVALS - 1,
      Math.ceil((ref.viewport.scrollLeft + ref.viewport.clientWidth) * scale) + 1,
    );
    return [start, end];
  }

  _scheduleDailyOperationPathRefresh() {
    if (this._dailyOperationPathRefreshPending) return;
    const refresh = () => {
      this._dailyOperationPathRefreshPending = false;
      if (this._dailyOperationSnapshot) {
        this._dailyOperationUpdatePaths(this._dailyOperationSnapshot);
      }
    };
    this._dailyOperationPathRefreshPending = true;
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(refresh);
    else refresh();
  }

  _dailyOperationPath(values, snapshot, yMax, future) {
    if (!Array.isArray(values)) return "";
    const top = 32, bottom = 164;
    const y = (value) => bottom - (this._clamp(value, 0, yMax) / yMax) * (bottom - top);
    const parts = [];
    let segment = [];
    const flush = () => {
      if (segment.length) parts.push(segment.join(" "));
      segment = [];
    };
    values.forEach((value, index) => {
      const number = this._dailyOperationNumber(value);
      const inRange = future ? index >= snapshot.currentIndex : index <= snapshot.currentIndex;
      if (snapshot.isSkipped[index] || number == null || !inRange) {
        flush();
        return;
      }
      const x = (index === snapshot.currentIndex ? index + snapshot.currentProgress : index + 0.5) * 10;
      segment.push(`${segment.length ? "L" : "M"}${x.toFixed(2)},${y(number).toFixed(2)}`);
    });
    flush();
    return parts.join(" ");
  }

  _dailyOperationUpdatePaths(snapshot) {
    const ref = this._r.dailyOperation;
    if (!ref) return;
    const actualSolar = this._dailyOperationPlotValues(snapshot.actualSolar, snapshot.solarCoverage, snapshot, false);
    const actualConsumption = this._dailyOperationPlotValues(snapshot.actualConsumption, snapshot.consumptionCoverage, snapshot, false);
    const forecastSolar = this._dailyOperationSolarForecastPlotValues(snapshot.forecastSolar, actualSolar, snapshot);
    const forecastConsumption = this._dailyOperationForecastPlotValues(snapshot.forecastConsumption, actualConsumption, snapshot);
    const projectedSoc = Array.isArray(snapshot.forecastSoc) ? snapshot.forecastSoc.slice() : snapshot.forecastSoc;
    const currentSoc = this._dailyOperationValueAt(snapshot.actualSoc, snapshot.currentIndex);
    if (Array.isArray(projectedSoc) && currentSoc != null) projectedSoc[snapshot.currentIndex] = currentSoc;
    const values = [actualSolar, forecastSolar, actualConsumption, forecastConsumption];
    const [visibleStart, visibleEnd] = this._dailyOperationVisibleRange();
    const yMax = Math.max(0.1, ...values.flatMap((array) => Array.isArray(array)
      ? array.slice(visibleStart, visibleEnd + 1)
        .map((value) => this._dailyOperationNumber(value))
        .filter((value) => value != null)
      : []
    )) * 1.12;
    ref.yAxis.innerHTML =
      `<small>${this._t("dailyOperationAxis")}</small>` +
      `<div class="daily-op-yaxis-ticks">` +
      [yMax, yMax * 0.75, yMax * 0.5, yMax * 0.25, 0]
        .map((value) => `<span>${this._nf(value, 2)}</span>`).join("") +
      `</div>`;
    const pathValues = [
      [ref.paths.solarActual, actualSolar, false],
      [ref.paths.solarForecast, forecastSolar, true],
      [ref.paths.consumptionActual, actualConsumption, false],
      [ref.paths.consumptionForecast, forecastConsumption, true],
    ];
    for (const [path, series, future] of pathValues) {
      const d = this._dailyOperationPath(series, snapshot, yMax, future);
      path.setAttribute("d", d);
      path.style.display = d ? "" : "none";
    }
    const socPaths = [
      [ref.paths.socActual, snapshot.actualSoc, false],
      [ref.paths.socForecast, projectedSoc, true],
    ];
    for (const [path, series, future] of socPaths) {
      const d = this._dailyOperationPath(series, snapshot, 100, future);
      path.setAttribute("d", d);
      path.style.display = d ? "" : "none";
    }
    const now = (snapshot.currentIndex + snapshot.currentProgress) * 10;
    ref.nowMarker.style.left = `${now / (DAILY_OPERATION_TOTAL_INTERVALS * 0.1)}%`;
    ref.nowText.textContent = this._t("dailyOperationNow");
  }

  _dailyOperationUpdateCell(snapshot, index) {
    const ref = this._r.dailyOperation;
    const cell = ref.cells[index];
    const item = this._dailyOperationItem(snapshot, index);
    // A solar window is the background opportunity. If another action (for
    // example a small discharge) overlaps it, keep the yellow window visible
    // and render the action as a hatch instead of replacing the window with
    // the action's solid colour.
    const baseAction = item.solarWindow
      ? "solar-window"
      : item.actions[0] || (item.decision === "not_needed" ? "not-needed" : "neutral");
    cell.className = `daily-op-cell daily-op-base-${baseAction}`;
    cell.classList.toggle("daily-op-real", item.status === "real");
    cell.classList.toggle("daily-op-current", item.status === "current");
    cell.classList.toggle("daily-op-forecast", item.status === "forecast");
    cell.classList.toggle("daily-op-delay", item.delay);
    cell.classList.toggle("daily-op-setpoint", item.setpoint);
    cell.classList.toggle("daily-op-hourly-balance", item.hourlyBalance);
    cell.classList.toggle("daily-op-dst-skipped", item.skipped);
    cell.classList.toggle("daily-op-dst-repeated", item.repeated);
    cell.classList.toggle("daily-op-stale", snapshot.stale && item.status === "forecast");
    const actionColor = {
      solar: "var(--daily-op-solar-charge)",
      grid: "var(--daily-op-grid)",
      discharge: "var(--daily-op-discharge)",
    };
    // SVG paint servers cannot be used as CSS backgrounds on HTML buttons.
    // Layer native CSS gradients instead: the base action keeps its fill and
    // each simultaneous action is always visible as an alternating hatch.
    const patternActions = item.solarWindow ? item.actions : item.actions.slice(1, 3);
    const patterns = patternActions.map((action, patternIndex) =>
      `repeating-linear-gradient(${patternIndex ? "45deg" : "135deg"}, transparent 0 4px, color-mix(in oklab, ${actionColor[action]} var(--daily-op-shade-opacity), transparent) 4px 6px)`
    );
    cell.style.backgroundImage = patterns.join(", ") || "none";
    cell.setAttribute("aria-label", this._dailyOperationAriaLabel(snapshot, item));
    cell.removeAttribute("title");
    const delayMark = cell.querySelector(".daily-op-delay-mark");
    const setpointMark = cell.querySelector(".daily-op-setpoint-mark");
    delayMark.hidden = !item.delay;
    setpointMark.hidden = !item.setpoint;
  }

  _patchDailyOperationTimeline() {
    const ref = this._r.dailyOperation;
    if (!ref) return;
    const snapshot = this._dailyOperationState();
    this._dailyOperationSnapshot = snapshot;
    if (!snapshot) {
      ref.card.hidden = true;
      this._hideDailyOperationTooltip();
      return;
    }
    const dayChanged = ref.localDate != null && snapshot.localDate != null
      && ref.localDate !== snapshot.localDate;
    ref.localDate = snapshot.localDate;
    if (dayChanged) {
      ref.scrollState.manual = false;
      ref.scrollState.initialized = false;
      ref.scrollState.programmaticUntil = Date.now() + 250;
      ref.viewport.scrollLeft = 0;
      this._dailyOpPinnedIndex = null;
      this._hideDailyOperationTooltip();
    }
    ref.card.hidden = false;
    const badgeText = `${this._t("dailyOperationReal")} / ${this._t("dailyOperationForecast")}`;
    ref.badge.querySelector("span:last-child").textContent = badgeText;
    ref.badge.classList.toggle("daily-op-badge-stale", snapshot.stale);
    const notices = [];
    if (snapshot.unavailable || !snapshot.hasValues) notices.push(this._t("dailyOperationNoData"));
    if (snapshot.stale) {
      const staleReason = this._dailyOperationStaleReasonLabel(snapshot.staleReason);
      notices.push(staleReason ? `${this._t("dailyOperationStale")}: ${staleReason}` : this._t("dailyOperationStale"));
    }
    for (const kind of ["solar", "consumption"]) {
      const reason = snapshot.fallbackReason(kind);
      const reasonLabel = this._dailyOperationFallbackReasonLabel(reason);
      if (reasonLabel) notices.push(this._t("dailyOperationFallback", { reason: reasonLabel }));
      const source = snapshot.source(kind);
      if (!reason && source && /fallback|sinus/i.test(source)) {
        notices.push(this._t("dailyOperationFallback", { reason: this._dailyOperationSourceLabel(source) }));
      }
    }
    if (snapshot.realtimeNoFuture) notices.push(this._t("dailyOperationRealtimeNoFuture"));
    ref.notice.textContent = notices.join(" · ");
    ref.notice.hidden = !notices.length;
    ref.viewport.setAttribute("aria-label", `${this._t("dailyOperationTitle")}. ${badgeText}`);
    for (let index = 0; index < DAILY_OPERATION_TOTAL_INTERVALS; index++) this._dailyOperationUpdateCell(snapshot, index);
    this._dailyOperationUpdatePaths(snapshot);
    this._centerDailyOperationNow(snapshot);
    this._updateDailyOperationNav();
    if (this._dailyOpPinnedIndex != null) {
      const cell = ref.cells[this._dailyOpPinnedIndex];
      if (cell) this._showDailyOperationTooltip(this._dailyOpPinnedIndex, cell);
    }
  }

  // ----- Flow card -----
  _buildFlowCard() {
    const { card, head } = this._card(this._t("cardFlow"), "mdi:transit-connection-variant");
    card.classList.add("flow-card");
    const livePill = document.createElement("span");
    livePill.className = "pill";
    livePill.style.marginLeft = "auto";
    livePill.innerHTML = `<span class="dot live"></span>${this._t("live")}`;
    head.appendChild(livePill);

    const wrap = document.createElement("div");
    wrap.className = "flow-wrap";
    const sq = document.createElement("div");
    sq.className = "scene-stage";

    // 3D-render backdrop + leader-line callouts (Tesla style). Lines are
    // axis-aligned (straight, or an L-elbow), never diagonal, and stop short of
    // the label text. Day/night renders are swapped by sun position.
    const sceneBase = new URL(".", import.meta.url);
    this._sceneDay = new URL("home-scene-day.png", sceneBase).href;
    this._sceneNight = new URL("home-scene-night.png", sceneBase).href;
    const GAP = 5; // % gap so the line ends before the label text

    // ex,ey = point on the render. lx,ly = label position.
    // shape: "v"  straight vertical (lx == ex)
    //        "hv" horizontal from element, then vertical down/up to the label
    //        "vh" vertical from element, then horizontal to the label
    const EDGES = [
      { key: "nGrid", edge: "grid", cap: this._t("grid"), ex: 38, ey: 63, lx: 12, ly: 9, shape: "hv" },
      { key: "nSolar", edge: "solar", cap: this._t("solar"), ex: 50, ey: 33, lx: 50, ly: 9, shape: "v" },
      { key: "nHome", edge: "home", cap: this._t("home"), ex: 66, ey: 48, lx: 88, ly: 9, shape: "hv" },
      { key: "nBatt", edge: "batt", cap: this._t("battery"), ex: 61, ey: 62, lx: 50, ly: 88, shape: "hv" },
      { key: "nExcl", edge: "excl", cap: this._t("excludedDevices"), ex: 80, ey: 70, lx: 88, ly: 88, shape: "hv", gap: 6 },
    ];
    const leadPts = (e) => {
      const g = e.gap ?? GAP; // per-edge override; defaults to the shared GAP
      if (e.shape === "hv") {
        const y2 = e.ly < e.ey ? e.ly + g : e.ly - g;
        return `${e.ex},${e.ey} ${e.lx},${e.ey} ${e.lx},${y2}`;
      }
      if (e.shape === "vh") {
        const x2 = e.lx < e.ex ? e.lx + g : e.lx - g;
        return `${e.ex},${e.ey} ${e.ex},${e.ly} ${x2},${e.ly}`;
      }
      const y2 = e.ly < e.ey ? e.ly + g : e.ly - g; // "v"
      return `${e.ex},${e.ey} ${e.ex},${y2}`;
    };

    const day = this._isDaytime();
    this._sceneIsDay = day;
    sq.innerHTML =
      `<img class="scene-img" src="${day ? this._sceneDay : this._sceneNight}" alt="" draggable="false">` +
      `<svg class="lead-svg" viewBox="0 0 100 100" preserveAspectRatio="none">` +
      EDGES.map(
        (e) =>
          `<polyline class="lead" data-edge="${e.edge}" points="${leadPts(e)}"/>` +
          `<polyline class="lead-flow" data-edge="${e.edge}" pathLength="100" points="${leadPts(e)}"/>` +
          `<circle class="lead-end" data-edge="${e.edge}" cx="${e.ex}" cy="${e.ey}" r="0.7"/>`
      ).join("") +
      `</svg>`;

    const img = sq.querySelector(".scene-img");
    img.addEventListener("error", () => {
      if (img.dataset.fb) return;
      img.dataset.fb = "1";
      img.src = new URL("home-scene.png", sceneBase).href; // single-image fallback
    });

    const node = (e) => {
      const n = document.createElement("div");
      n.className = "scene-lbl l-" + e.edge;
      n.style.left = e.lx + "%";
      n.style.top = e.ly + "%";
      n.innerHTML =
        `<div class="lbl-val num"><span class="fn-v">—</span><span class="fn-unit"></span></div>` +
        `<div class="lbl-cap pf-label">${e.cap}</div>` +
        `<div class="lbl-badge pf-badge"></div>`;
      sq.appendChild(n);
      this._r[e.key] = {
        node: n,
        val: n.querySelector(".fn-v"),
        unit: n.querySelector(".fn-unit"),
        label: n.querySelector(".pf-label"),
        badge: n.querySelector(".pf-badge"),
      };
    };
    EDGES.forEach(node);
    // click a flow node -> more-info (history graph). Grid/Solar/Home map to the
    // configured sensors. Battery: use the signed system cell-power aggregate when
    // available (shows total charge+discharge in one graph), else system charge power.
    const fcfg = this._panelConfig;
    const battEid = this._sysEntityId(K.sysBattCellPower) || this._sysEntityId(K.sysChargePower);
    this._linkMoreInfo(this._r.nGrid.node, fcfg.grid_entity);
    this._linkMoreInfo(this._r.nSolar.node, fcfg.solar_entity);
    // home: resolved dynamically (config entity_id if it exists, else translation_key)
    // so clicks still work after an entity rename without an integration reload.
    this._linkMoreInfo(this._r.nHome.node, this._homeEntityId(this._hass));
    this._linkMoreInfo(this._r.nBatt.node, battEid);

    // self-consumption chip, bottom-centre of the scene
    const self = document.createElement("div");
    self.className = "scene-self";
    self.innerHTML = `<span class="hub-self">—</span>${this._t("selfConsumptionSuffix")}`;
    sq.appendChild(self);

    wrap.appendChild(sq);
    card.appendChild(wrap);

    this._r.flowSvg = sq; // satisfies the "on Resumen" guard in _patch
    this._r.hubSelf = self.querySelector(".hub-self");
    this._r.sceneImg = img;
    this._r.wires = {}; // no animated wires in scene mode
    this._r.dots = {}; // no particles in scene mode
    this._r.leads = {};
    sq.querySelectorAll(".lead, .lead-end").forEach((el) => {
      (this._r.leads[el.dataset.edge] = this._r.leads[el.dataset.edge] || []).push(el);
    });
    this._r.flows = {}; // animated "snake" polyline per edge (color + direction by state)
    sq.querySelectorAll(".lead-flow").forEach((el) => {
      (this._r.flows[el.dataset.edge] = this._r.flows[el.dataset.edge] || []).push(el);
    });
    return card;
  }

  /** Daytime if the sun is up; falls back to a local-hour heuristic. */
  _isDaytime() {
    const sun = this._hass && this._hass.states && this._hass.states["sun.sun"];
    if (sun) return sun.state !== "below_horizon";
    const h = this._localHour();
    return h >= 7 && h < 20;
  }

  /** Scene day/night driven by solar production: night once PV stops (< 50 W).
   *  Hysteresis (50 W off / 80 W on) prevents flicker on passing clouds. Falls
   *  back to sun position / local hour when no solar sensor is configured. */
  _sceneDaytime(m) {
    if (m && m.hasSolar && m.solar != null) {
      const w = m.solar * 1000;
      if (this._sceneIsDay && w < 50) return false;
      if (!this._sceneIsDay && w >= 80) return true;
      return this._sceneIsDay;
    }
    return this._isDaytime();
  }

  /** Rebuild the animated particles for one flow edge (only when its bucket/dir changes). */
  _patchEdge(edge, pathId, color, active, reversed, mag) {
    const n = active ? this._clamp(Math.round(Math.abs(mag) * 1.8) + 1, 1, 5) : 0;
    const dur = this._clamp(2.6 - Math.abs(mag) * 0.28, 0.75, 2.6);
    const sig = `${active ? 1 : 0}|${reversed ? 1 : 0}|${n}|${dur.toFixed(2)}`;
    if (this._edgeSig[edge] === sig) return;
    this._edgeSig[edge] = sig;

    const g = this._r.dots[edge];
    if (!g) return; // scene mode: no particle layer
    g.textContent = "";
    if (!active) return;
    const SVG = "http://www.w3.org/2000/svg";
    const XLINK = "http://www.w3.org/1999/xlink";
    for (let i = 0; i < n; i++) {
      const c = document.createElementNS(SVG, "circle");
      c.setAttribute("r", "1.7");
      c.setAttribute("fill", color);
      c.style.filter = `drop-shadow(0 0 3px ${color})`;
      const m = document.createElementNS(SVG, "animateMotion");
      m.setAttribute("dur", dur + "s");
      m.setAttribute("repeatCount", "indefinite");
      m.setAttribute("begin", -(i * dur) / n + "s");
      m.setAttribute("keyPoints", reversed ? "1;0" : "0;1");
      m.setAttribute("keyTimes", "0;1");
      m.setAttribute("calcMode", "linear");
      const mp = document.createElementNS(SVG, "mpath");
      mp.setAttribute("href", "#" + pathId);
      mp.setAttributeNS(XLINK, "xlink:href", "#" + pathId);
      m.appendChild(mp);
      c.appendChild(m);
      g.appendChild(c);
    }
  }

  // ----- SOC hero: ring (SOC + capacity + system power) left, diagnostics right -----
  _buildSocCard() {
    const { card } = this._card(this._t("cardSoc"), "mdi:battery-charging-high");
    card.classList.add("soc-card");
    const size = 224, stroke = 16, pad = 12; // pad leaves room for the glow so it isn't clipped by the svg box
    const r = (size - stroke) / 2 - pad;
    const circ = 2 * Math.PI * r;
    const ring = document.createElement("div");
    ring.className = "ring";
    ring.style.width = size + "px";
    ring.style.height = size + "px";
    ring.innerHTML = `
      <svg width="${size}" height="${size}" style="transform:rotate(-90deg)">
        <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--bg-2)" stroke-width="${stroke}"/>
        <circle class="ring-fg" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--battery)"
          stroke-width="${stroke}" stroke-linecap="round"
          stroke-dasharray="${circ.toFixed(2)}" stroke-dashoffset="${circ.toFixed(2)}"/>
      </svg>
      <div class="ring-center">
        <div class="num ring-val">—<span>%</span></div>
        <div class="dim ring-sub">— / — kWh</div>
      </div>`;

    // System charge / discharge power + available headroom, under the ring.
    const pw = document.createElement("div");
    pw.className = "soc-power";
    pw.innerHTML = `
      <div class="pw-stats">
        <div class="statblock">
          <div class="stat-label"><ha-icon icon="mdi:plus"></ha-icon>${this._t("charge")}</div>
          <div class="stat-value pw-charge" style="color:var(--battery)">—<span class="stat-unit"></span></div>
        </div>
        <div class="statblock" style="text-align:right">
          <div class="stat-label" style="justify-content:flex-end"><ha-icon icon="mdi:minus"></ha-icon>${this._t("discharge")}</div>
          <div class="stat-value pw-disch" style="color:var(--grid)">—<span class="stat-unit"></span></div>
        </div>
      </div>
      <div class="socbar" style="height:6px;margin-top:9px"><span class="pw-bar"></span></div>
      <div class="dim pw-avail">—</div>`;

    // Left — ring (SOC + capacity) and the power block.
    const left = document.createElement("div");
    left.className = "soc-left";
    left.appendChild(ring);
    left.appendChild(pw);

    // Right — diagnostics, two columns, same height as the ring column.
    const inner = document.createElement("div");
    inner.className = "soc-inner";
    inner.appendChild(left);
    inner.appendChild(this._buildDiagBody());
    card.appendChild(inner);

    // click ring / capacity / power blocks -> more-info (history graph)
    this._linkMoreInfo(ring, this._sysEntityId(K.sysSoc));
    this._linkMoreInfo(ring.querySelector(".ring-sub"), this._sysEntityId(K.sysStored));
    const sb = pw.querySelectorAll(".statblock");
    // On vA/vD the Charge/Discharge blocks show cell power (AC + DC MPPT), so link
    // the matching signed cell-power sensor when it exists; it's only created for
    // MPPT systems, so others fall back to the AC-only charge/discharge sensors (#347).
    const cellId = this._sysEntityId(K.sysBattCellPower);
    this._linkMoreInfo(sb[0], cellId || this._sysEntityId(K.sysChargePower));
    this._linkMoreInfo(sb[1], cellId || this._sysEntityId(K.sysDischargePower));

    this._r.ringFg = ring.querySelector(".ring-fg");
    this._r.ringCirc = circ;
    this._r.ringVal = ring.querySelector(".ring-val");
    this._r.ringSub = ring.querySelector(".ring-sub");
    this._r.pwCharge = pw.querySelector(".pw-charge");
    this._r.pwDisch = pw.querySelector(".pw-disch");
    this._r.pwBar = pw.querySelector(".pw-bar");
    this._r.pwAvail = pw.querySelector(".pw-avail");
    return card;
  }

  // ----- Daily energy card -----
  _buildDailyCard() {
    const { card } = this._card(this._t("cardDaily"), "mdi:calendar-today");
    card.classList.add("daily-card");
    const body = document.createElement("div");
    body.className = "daily-body";
    const bar = (cls, label, color) => `
      <div class="daily-row daily-row-${cls}">
        <div class="daily-head"><span class="muted">${label}</span>
          <span class="num daily-${cls}-v">—<span class="dim" style="font-size:11px"> kWh</span></span></div>
        <div class="socbar"><span class="daily-${cls}-bar" style="background:${color}"></span></div>
      </div>`;
    body.innerHTML =
      bar("ch", this._t("charged"), "var(--battery)") +
      bar("dis", this._t("discharged"), "var(--grid)") +
      bar("sol", this._t("solar"), "var(--solar)") +
      bar("home", this._t("home"), "var(--home)") +
      bar("imp", this._t("gridImport"), "var(--flow-purple)") +
      bar("exp", this._t("gridExport"), "var(--flow-orange)") +
      bar("forecast", this._t("forecastToday"), "var(--solar)") +
      bar("remaining", this._t("solarRemaining"), "var(--solar)") +
      bar("expected", this._t("expectedConsumption"), "var(--home)");
    card.appendChild(body);
    const rows = body.querySelectorAll(".daily-row");
    // click an energy row -> more-info (history graph)
    this._linkMoreInfo(rows[0], this._sysEntityId(K.sysDailyCharge));
    this._linkMoreInfo(rows[1], this._sysEntityId(K.sysDailyDischarge));
    this._linkMoreInfo(rows[2], this._sysEntityId(K.sysDailySolar));
    this._linkMoreInfo(rows[3], this._sysEntityId(K.sysDailyHome));
    this._linkMoreInfo(rows[4], this._sysEntityId(K.sysDailyGridImport));
    this._linkMoreInfo(rows[5], this._sysEntityId(K.sysDailyGridExport));
    this._linkMoreInfo(rows[6], this._sysEntityId(K.predictiveActive));
    this._linkMoreInfo(rows[7], this._sysEntityId(K.predictiveActive));
    this._linkMoreInfo(rows[8], this._sysEntityId(K.consumptionProfile));
    this._r.dChV = body.querySelector(".daily-ch-v");
    this._r.dChBar = body.querySelector(".daily-ch-bar");
    this._r.dDisV = body.querySelector(".daily-dis-v");
    this._r.dDisBar = body.querySelector(".daily-dis-bar");
    this._r.dSolRow = rows[2];
    this._r.dSolV = body.querySelector(".daily-sol-v");
    this._r.dSolBar = body.querySelector(".daily-sol-bar");
    this._r.dHomeRow = rows[3];
    this._r.dHomeV = body.querySelector(".daily-home-v");
    this._r.dHomeBar = body.querySelector(".daily-home-bar");
    this._r.dImpRow = rows[4];
    this._r.dImpV = body.querySelector(".daily-imp-v");
    this._r.dImpBar = body.querySelector(".daily-imp-bar");
    this._r.dExpRow = rows[5];
    this._r.dExpV = body.querySelector(".daily-exp-v");
    this._r.dExpBar = body.querySelector(".daily-exp-bar");
    this._r.dForecastRow = rows[6];
    this._r.dForecastV = body.querySelector(".daily-forecast-v");
    this._r.dForecastBar = body.querySelector(".daily-forecast-bar");
    this._r.dRemainingRow = rows[7];
    this._r.dRemainingV = body.querySelector(".daily-remaining-v");
    this._r.dRemainingBar = body.querySelector(".daily-remaining-bar");
    this._r.dExpectedRow = rows[8];
    this._r.dExpectedV = body.querySelector(".daily-expected-v");
    this._r.dExpectedBar = body.querySelector(".daily-expected-bar");
    return card;
  }

  // ----- Mini SOC history -----
  _buildMiniHistory() {
    const { card, head } = this._card(this._t("cardSocToday"), "mdi:chart-areaspline");
    card.classList.add("chart-card");
    const pct = document.createElement("span");
    pct.className = "num dim mini-pct";
    pct.style.marginLeft = "auto";
    pct.style.fontSize = "13px";
    pct.textContent = "—";
    head.appendChild(pct);

    const sparkWrap = document.createElement("div");
    sparkWrap.className = "mini-spark chart-canvas";
    sparkWrap.innerHTML =
      `<div class="chart-yaxis">${this._yAxisHTML({ yMin: 0, yMax: 100, unit: "%", decimals: 0 })}</div>` +
      `<div class="chart-surface"><svg viewBox="0 0 280 68" width="100%" height="100%" preserveAspectRatio="none">
          <defs><linearGradient id="mv-spark" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="var(--accent)" stop-opacity="0.28"/>
            <stop offset="1" stop-color="var(--accent)" stop-opacity="0"/>
          </linearGradient></defs>
          <line class="chart-grid" x1="0" y1="0" x2="280" y2="0"/>
          <line class="chart-grid" x1="0" y1="17" x2="280" y2="17"/>
          <line class="chart-grid" x1="0" y1="34" x2="280" y2="34"/>
          <line class="chart-grid" x1="0" y1="51" x2="280" y2="51"/>
          <line class="chart-grid" x1="0" y1="68" x2="280" y2="68"/>
          <path class="spark-area" fill="url(#mv-spark)"></path>
          <path class="spark-line" fill="none" stroke="var(--accent)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"></path>
        </svg></div>`;
    const axis = document.createElement("div");
    axis.className = "mini-axis dim";
    axis.innerHTML = `<span>00:00</span><span>${this._t("now")}</span>`;

    card.appendChild(sparkWrap);
    card.appendChild(axis);
    card.appendChild(this._buildZoomBar(sparkWrap, "soc"));
    this._r.miniPct = pct;
    this._r.miniSpark = sparkWrap;
    this._r.miniAxis = axis;
    this._r.sparkArea = sparkWrap.querySelector(".spark-area");
    this._r.sparkLine = sparkWrap.querySelector(".spark-line");
    this._attachHover(sparkWrap);
    this._attachBrush(sparkWrap, "soc");
    this._drawSpark();
    return card;
  }

  _drawSpark() {
    if (!this._r.sparkLine) return;
    const host = this._r.miniSpark;
    const full = this._socSeries;
    if (!full || full.length < 2) {
      this._r.sparkLine.setAttribute("d", "");
      this._r.sparkArea.setAttribute("d", "");
      if (host) host.__hv = null;
      this._updateMiniAxis(null);
      return;
    }
    // SOC samples are evenly spaced from 00:00 → now; map an original index → clock.
    const startS = this._dayStartEpoch() / 1000;
    const elapsed = Date.now() / 1000 - startS;
    const fullLast = full.length - 1;
    const clockOf = (origIdx) => startS + (fullLast > 0 ? origIdx / fullLast : 0) * elapsed;

    // apply zoom (fraction of the index domain), if set
    const z = host && host.__zoom;
    let data = full, i0 = 0;
    if (z) {
      i0 = Math.round(z.lo * fullLast);
      const i1 = Math.round(z.hi * fullLast);
      if (i1 - i0 >= 1) data = full.slice(i0, i1 + 1);
      else i0 = 0;
    }

    const w = 280, h = 68, lo = 0, hi = 100, rng = hi - lo;
    const pts = data.map((d, i) => [
      (i / (data.length - 1)) * w,
      h - ((this._clamp(d, lo, hi) - lo) / rng) * h,
    ]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    this._r.sparkLine.setAttribute("d", line);
    this._r.sparkArea.setAttribute("d", `${line} L${w} ${h} L0 ${h} Z`);
    if (this._r.miniPct) this._r.miniPct.textContent = Math.round(full[full.length - 1]) + "%";
    if (host) {
      host.__hv = {
        kind: "line",
        n: data.length,
        xs: null,
        series: [{ label: "SOC", color: "var(--accent)", data: data.slice() }],
        yMin: 0,
        yMax: 100,
        unit: "%",
        decimals: 0,
        xLabel: (k) => this._fmtClock(clockOf(i0 + k)),
      };
    }
    this._updateMiniAxis(z ? { t0: clockOf(i0), t1: clockOf(i0 + data.length - 1) } : null);
  }

  // ----- Inline SVG chart helpers (ported from the design handoff) -----
  /** Multi-series line chart as an SVG string. Shapes only (no SVG text, so
   *  preserveAspectRatio="none" can stretch it to the card height without
   *  distorting labels); non-scaling-stroke keeps line widths constant. */
  _lineChartSVG({ series, yMin, yMax, xs }) {
    const W = 320, H = 160;
    const n = Math.max(0, ...series.map((s) => (s.data ? s.data.length : 0)));
    if (n < 2) return "";
    const span = yMax - yMin || 1;
    // xs: optional per-sample x positions in [0,1] (e.g. fraction of the day),
    // so a partial day of data sits at its real clock position instead of being
    // stretched across the full width. Falls back to even index spacing.
    const X = (i) => (xs ? this._clamp(xs[i], 0, 1) : i / (n - 1)) * W;
    const Y = (v) => H - ((this._clamp(v, yMin, yMax) - yMin) / span) * H;
    let g = "";
    for (let k = 0; k <= 4; k++) {
      const y = ((k / 4) * H).toFixed(1);
      g += `<line class="chart-grid" x1="0" y1="${y}" x2="${W}" y2="${y}"/>`;
    }
    if (yMin < 0 && yMax > 0) {
      const yz = Y(0).toFixed(1);
      g += `<line class="chart-zero" x1="0" y1="${yz}" x2="${W}" y2="${yz}"/>`;
    }
    let paths = "";
    for (const s of series) {
      if (!s.data || s.data.length < 2) continue;
      const d = s.data
        .map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1))
        .join(" ");
      paths +=
        `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" ` +
        `vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`;
    }
    return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="100%">${g}${paths}</svg>`;
  }

  _axisDecimals(yMin, yMax) {
    const span = Math.abs(yMax - yMin);
    return span < 1 ? 2 : span < 10 ? 1 : 0;
  }

  _yAxisHTML({ yMin, yMax, unit, decimals = this._axisDecimals(yMin, yMax) }) {
    return Array.from({ length: 5 }, (_, i) => {
      const raw = yMax - ((yMax - yMin) * i) / 4;
      const value = Math.abs(raw) < 10 ** -(decimals + 1) ? 0 : raw;
      return `<span>${this._nf(value, decimals)}<small>${unit}</small></span>`;
    }).join("");
  }

  _chartWithYAxis(svg, { yMin, yMax, unit, decimals }) {
    return (
      `<div class="chart-canvas">` +
      `<div class="chart-yaxis">${this._yAxisHTML({ yMin, yMax, unit, decimals })}</div>` +
      `<div class="chart-surface">${svg}</div>` +
      `</div>`
    );
  }

  // ----- Chart hover readout (crosshair + value tooltip) -----------------
  /** Profile-formatted local clock for an epoch-seconds value. */
  _fmtClock(s) {
    if (s == null) return "";
    return new Date(s * 1000).toLocaleTimeString(
      this._lang(), this._dateTimeOptions({
        hour: this._useAmPm() ? "numeric" : "2-digit",
        minute: "2-digit",
      })
    );
  }

  /** Attach a hover readout to a STABLE element (the .chart-plot or .mini-spark
   *  wrapper). The per-draw model lives on `host.__hv`; the overlay nodes are
   *  (re)created inside the live .chart-surface so they survive the innerHTML
   *  rebuilds that happen on every data refresh. */
  _attachHover(host) {
    if (host.__hoverBound) return;
    host.__hoverBound = true;
    const hide = () => { if (host.__ov) host.__ov.root.style.display = "none"; };
    host.addEventListener("mouseleave", hide);
    host.addEventListener("mousemove", (ev) => {
      if (host.__dragging) return hide();
      const hv = host.__hv;
      const surface = host.querySelector(".chart-surface");
      if (!hv || !surface) return hide();
      const rect = surface.getBoundingClientRect();
      if (rect.width <= 0) return hide();
      const fx = this._clamp((ev.clientX - rect.left) / rect.width, 0, 1);
      let ov = host.__ov;
      if (!ov || ov.surface !== surface || !surface.contains(ov.root)) {
        ov = this._makeHoverOverlay(surface);
        host.__ov = ov;
      }
      ov.root.style.display = "block";
      (hv.kind === "bar" ? this._hoverBar : this._hoverLine).call(this, hv, fx, rect, ov);
    });
  }

  _makeHoverOverlay(surface) {
    const root = document.createElement("div");
    root.className = "chart-hover";
    root.innerHTML = `<div class="hv-line"></div><div class="hv-dots"></div><div class="hv-tip"></div>`;
    surface.appendChild(root);
    return {
      surface, root,
      line: root.querySelector(".hv-line"),
      dots: root.querySelector(".hv-dots"),
      tip: root.querySelector(".hv-tip"),
    };
  }

  _hoverRow(color, label, valueHTML) {
    return (
      `<div class="hv-r"><span class="hv-k"><i style="background:${color}"></i>${label}</span>` +
      `<span class="hv-v">${valueHTML}</span></div>`
    );
  }

  _placeTip(ov, rect, leftPx, headHTML, rows) {
    ov.line.style.left = leftPx.toFixed(1) + "px";
    ov.tip.innerHTML = `<div class="hv-h">${headHTML}</div>` + rows.join("");
    const tw = ov.tip.offsetWidth || 0;
    let tl = leftPx + 12;
    if (tl + tw > rect.width) tl = leftPx - 12 - tw;
    ov.tip.style.left = this._clamp(tl, 0, Math.max(0, rect.width - tw)).toFixed(1) + "px";
  }

  _hoverLine(hv, fx, rect, ov) {
    const n = hv.n;
    if (!n) return;
    const frac = (k) => (hv.xs ? this._clamp(hv.xs[k], 0, 1) : n > 1 ? k / (n - 1) : 0);
    let bi = 0, bd = Infinity;
    for (let k = 0; k < n; k++) { const d = Math.abs(frac(k) - fx); if (d < bd) { bd = d; bi = k; } }
    const leftPx = frac(bi) * rect.width;
    const span = hv.yMax - hv.yMin || 1;
    let dots = "";
    const rows = [];
    for (const s of hv.series) {
      const v = s.data ? s.data[bi] : null;
      if (v == null || Number.isNaN(v)) continue;
      const top = (1 - (this._clamp(v, hv.yMin, hv.yMax) - hv.yMin) / span) * rect.height;
      dots += `<span class="hv-dot" style="left:${leftPx.toFixed(1)}px;top:${top.toFixed(1)}px;background:${s.color}"></span>`;
      rows.push(this._hoverRow(s.color, s.label, `${this._nf(v, hv.decimals)} ${hv.unit}`));
    }
    ov.dots.innerHTML = dots;
    this._placeTip(ov, rect, leftPx, hv.xLabel(bi), rows);
  }

  _hoverBar(hv, fx, rect, ov) {
    const c = hv.count;
    if (!c) return;
    const li = this._clamp(Math.floor(fx * c), 0, c - 1);
    const leftPx = ((li + 0.5) / c) * rect.width;
    ov.dots.innerHTML = "";
    const rows = hv.groups.map((g) =>
      this._hoverRow(g.color, g.label, `${this._nf(g.values[li] || 0, hv.decimals)} ${hv.unit}`)
    );
    this._placeTip(ov, rect, leftPx, hv.xLabel(li), rows);
  }

  /** Grouped bar chart as an SVG string plus its calculated Y maximum. */
  _barChartSVG({ groups, count }) {
    const W = 320, H = 160;
    const all = groups.flatMap((g) => g.values.map((v) => v || 0));
    const yMax = Math.max(0.1, ...all) * 1.12;
    const Y = (v) => H - (Math.max(0, v) / yMax) * H;
    const slot = W / Math.max(1, count);
    const ng = groups.length;
    // thinner bars: cap per-bar width low and keep the cluster compact so 4
    // series per day still read clearly with gaps between them
    const bw = Math.min(slot * 0.18, (slot * 0.66) / ng);
    let grid = "";
    for (let k = 0; k <= 4; k++) {
      const y = ((k / 4) * H).toFixed(1);
      grid += `<line class="chart-grid" x1="0" y1="${y}" x2="${W}" y2="${y}"/>`;
    }
    let rects = "";
    for (let li = 0; li < count; li++) {
      groups.forEach((grp, gi) => {
        const v = grp.values[li] || 0;
        const x = slot * li + slot / 2 - (ng * bw) / 2 + gi * bw;
        const y = Y(v);
        rects +=
          `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw - 1.5).toFixed(1)}" ` +
          `height="${(H - y).toFixed(1)}" rx="2" fill="${grp.color}"/>`;
      });
    }
    return {
      svg: `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="100%">${grid}${rects}</svg>`,
      yMax,
    };
  }

  _legendHTML(items) {
    return items
      .map(
        (it) =>
          `<span class="legend-item"><span class="legend-dot" style="background:${it.color}"></span>${it.label}</span>`
      )
      .join("");
  }

  // ----- Potencias (24 h, up to 4 series) -----
  _buildPowerHistoryCard() {
    const { card, head } = this._card(this._t("cardPower"), "mdi:flash");
    card.classList.add("chart-card");
    const legend = document.createElement("span");
    legend.className = "chart-legend";
    legend.style.marginLeft = "auto";
    head.appendChild(legend);
    const plot = document.createElement("div");
    plot.className = "chart-plot";
    const xaxis = document.createElement("div");
    xaxis.className = "chart-xaxis dim";
    xaxis.innerHTML = `<span>00</span><span>06</span><span>12</span><span>18</span><span>24</span>`;
    card.appendChild(plot);
    card.appendChild(xaxis);
    card.appendChild(this._buildZoomBar(plot, "power"));
    this._r.powerLegend = legend;
    this._r.powerPlot = plot;
    this._r.powerXaxis = xaxis;
    this._attachHover(plot);
    this._attachBrush(plot, "power");
    this._drawPowerHistory();
    return card;
  }

  _drawPowerHistory() {
    const plot = this._r.powerPlot;
    if (!plot) return;
    const ps = this._powerSeries;
    const defs = [
      { key: "solar", label: this._t("solar"), color: "var(--solar)" },
      { key: "home", label: this._t("home"), color: "var(--home)" },
      { key: "battery", label: this._t("battery"), color: "var(--battery)" },
      { key: "grid", label: this._t("grid"), color: "var(--grid)" },
    ];
    const avail = ps
      ? defs.filter((d) => Array.isArray(ps[d.key]) && ps[d.key].some((v) => v != null))
      : [];
    if (this._r.powerLegend) this._r.powerLegend.innerHTML = this._legendHTML(avail);
    if (!avail.length) {
      plot.innerHTML = `<div class="chart-empty dim">${this._t("noData")}</div>`;
      plot.__hv = null;
      return;
    }
    const fullSeries = avail.map((d) => ({ color: d.color, data: ps[d.key].map((v) => (v == null ? 0 : v)) }));
    // Anchor each sample to its real time-of-day so a partial day (e.g. 03:00)
    // only fills the left of the fixed 00–24 axis instead of stretching across it.
    const startS = this._dayStartEpoch() / 1000;
    const endS = this._dayStartEpoch(1) / 1000;
    const daySpan = endS - startS;
    const t = Array.isArray(ps.t) ? ps.t : null;
    const fullXs = t && t.length ? t.map((ts) => (ts - startS) / daySpan) : null;

    // apply the active zoom window (fraction of the 24 h day), if set
    const z = plot.__zoom;
    let series = fullSeries, xs = fullXs, times = t;
    if (z && fullXs) {
      const idx = [];
      for (let i = 0; i < fullXs.length; i++) if (fullXs[i] >= z.lo && fullXs[i] <= z.hi) idx.push(i);
      if (idx.length >= 2) {
        const sp = z.hi - z.lo || 1;
        xs = idx.map((i) => (fullXs[i] - z.lo) / sp);
        series = fullSeries.map((s) => ({ color: s.color, data: idx.map((i) => s.data[i]) }));
        times = t ? idx.map((i) => t[i]) : null;
      }
    }

    let lo = 0, hi = 0;
    for (const s of series) for (const v of s.data) { if (v < lo) lo = v; if (v > hi) hi = v; }
    const pad = (hi - lo) * 0.08 || 0.2;
    const yMin = lo - pad;
    const yMax = hi + pad;
    plot.innerHTML = this._chartWithYAxis(this._lineChartSVG({ series, yMin, yMax, xs }), {
      yMin,
      yMax,
      unit: "kW",
    });
    plot.__hv = {
      kind: "line",
      n: Math.max(0, ...series.map((s) => s.data.length)),
      xs,
      series: avail.map((d, i) => ({ label: d.label, color: d.color, data: series[i].data })),
      yMin,
      yMax,
      unit: "kW",
      decimals: 2,
      xLabel: (i) => (times ? this._fmtClock(times[i]) : ""),
    };
    this._updatePowerXaxis(z, startS, endS);
  }

  // ----- Energía semanal (7 días, barras agrupadas) -----
  _buildWeeklyCard() {
    const { card, head } = this._card(this._t("cardWeekly"), "mdi:calendar-week");
    card.classList.add("chart-card");
    const legend = document.createElement("span");
    legend.className = "chart-legend";
    legend.style.marginLeft = "auto";
    head.appendChild(legend);
    const plot = document.createElement("div");
    plot.className = "chart-plot";
    const xaxis = document.createElement("div");
    xaxis.className = "chart-xaxis dim";
    card.appendChild(plot);
    card.appendChild(xaxis);
    this._r.weeklyPlot = plot;
    this._r.weeklyXaxis = xaxis;
    this._r.weeklyLegend = legend;
    this._attachHover(plot);
    this._drawWeekly();
    return card;
  }

  _drawWeekly() {
    const plot = this._r.weeklyPlot;
    if (!plot) return;
    const wk = this._weekly;
    if (!wk || !wk.days || !wk.days.length) {
      plot.innerHTML = `<div class="chart-empty dim">${this._t("noData")}</div>`;
      plot.__hv = null;
      if (this._r.weeklyXaxis) this._r.weeklyXaxis.innerHTML = "";
      if (this._r.weeklyLegend) this._r.weeklyLegend.innerHTML = "";
      return;
    }
    const groups = [
      { label: this._t("charge"), color: "var(--battery)", values: wk.charge },
      { label: this._t("discharge"), color: "var(--grid)", values: wk.discharge },
    ];
    if (wk.import) groups.push({ label: this._t("imported"), color: "var(--flow-purple)", values: wk.import });
    if (wk.export) groups.push({ label: this._t("exported"), color: "var(--flow-orange)", values: wk.export });
    if (this._r.weeklyLegend) this._r.weeklyLegend.innerHTML = this._legendHTML(groups);
    const { svg, yMax } = this._barChartSVG({ groups, count: wk.days.length });
    plot.innerHTML = this._chartWithYAxis(svg, { yMin: 0, yMax, unit: "kWh" });
    plot.__hv = {
      kind: "bar",
      count: wk.days.length,
      groups,
      unit: "kWh",
      decimals: 2,
      xLabel: (i) => wk.days[i] || "",
    };
    if (this._r.weeklyXaxis) this._r.weeklyXaxis.innerHTML = wk.days.map((d) => `<span>${d}</span>`).join("");
  }

  // ----- Chart zoom (drag-to-brush on desktop + range buttons everywhere) -----
  _zoomHostFor(kind) {
    return kind === "power" ? this._r.powerPlot : this._r.miniSpark;
  }
  _redrawChart(kind) {
    if (kind === "power") this._drawPowerHistory();
    else this._drawSpark();
  }
  /** Natural time domain (epoch s) for a chart: Potencias spans the full day,
   *  SOC spans midnight -> now. */
  _chartDomain(kind) {
    const startS = this._dayStartEpoch() / 1000;
    const nowS = Date.now() / 1000;
    return {
      startS,
      nowS,
      endS: kind === "power" ? this._dayStartEpoch(1) / 1000 : nowS,
    };
  }
  /** Range-preset buttons + reset, placed under the chart. */
  _buildZoomBar(host, kind) {
    const bar = document.createElement("div");
    bar.className = "chart-zoom";
    const opts = [["1h", 1], ["6h", 6], ["12h", 12], [this._t("zoomReset"), null]];
    for (const [label, h] of opts) {
      const b = document.createElement("button");
      b.className = "zoom-btn";
      b.textContent = label;
      b.dataset.h = h == null ? "" : String(h);
      b.addEventListener("click", () => this._setRangeHours(host, kind, h));
      bar.appendChild(b);
    }
    host.__zoomBar = bar;
    host.__activeH = null; // full view
    const resetBtn = bar.querySelector('.zoom-btn[data-h=""]');
    if (resetBtn) resetBtn.classList.add("active");
    return bar;
  }
  /** Set the window to the last `hours` ending at now (null = full/reset). */
  _setRangeHours(host, kind, hours) {
    if (hours == null) {
      host.__zoom = null;
      host.__activeH = null;
    } else {
      const { startS, endS, nowS } = this._chartDomain(kind);
      const span = endS - startS || 1;
      const lo = this._clamp((nowS - hours * 3600 - startS) / span, 0, 1);
      const hi = this._clamp((nowS - startS) / span, 0, 1);
      host.__zoom = hi - lo > 0.005 ? { lo, hi } : null;
      host.__activeH = host.__zoom ? hours : null;
    }
    this._redrawChart(kind);
    this._syncZoomBtns(kind);
  }
  _syncZoomBtns(kind) {
    const host = this._zoomHostFor(kind);
    if (!host || !host.__zoomBar) return;
    const active = host.__activeH;
    host.__zoomBar.querySelectorAll(".zoom-btn").forEach((b) => {
      const h = b.dataset.h === "" ? null : Number(b.dataset.h);
      b.classList.toggle("active", h === active);
    });
  }
  _makeBrushBox(surface) {
    const box = document.createElement("div");
    box.className = "brush-box";
    surface.appendChild(box);
    return box;
  }
  /** Compose a brush selection (fraction of the visible width) with the current
   *  zoom to produce the new absolute window. */
  _applyBrush(host, kind, f0, f1) {
    const z = host.__zoom || { lo: 0, hi: 1 };
    const sp = z.hi - z.lo || 1;
    const lo = z.lo + f0 * sp;
    const hi = z.lo + f1 * sp;
    if (hi - lo < 0.01) return;
    host.__zoom = { lo, hi };
    host.__activeH = "custom"; // no preset highlighted
    this._redrawChart(kind);
    this._syncZoomBtns(kind);
  }
  /** Drag-to-zoom with the mouse (pointer). Touch is left to scroll + the range
   *  buttons, so touch pointers are ignored here. */
  _attachBrush(host, kind) {
    if (host.__brushBound) return;
    host.__brushBound = true;
    let startX = null, box = null, rect = null, pid = null;
    const onDown = (ev) => {
      if (ev.pointerType === "touch") return;
      const surface = host.querySelector(".chart-surface");
      if (!surface) return;
      rect = surface.getBoundingClientRect();
      if (rect.width <= 0) return;
      startX = this._clamp((ev.clientX - rect.left) / rect.width, 0, 1);
      host.__dragging = true;
      box = this._makeBrushBox(surface);
      box.style.left = (startX * 100).toFixed(2) + "%";
      box.style.width = "0%";
      pid = ev.pointerId;
      try { host.setPointerCapture(pid); } catch (e) { /* ignore */ }
    };
    const onMove = (ev) => {
      if (startX == null || !box || !rect) return;
      const cx = this._clamp((ev.clientX - rect.left) / rect.width, 0, 1);
      const l = Math.min(startX, cx), r = Math.max(startX, cx);
      box.style.left = (l * 100).toFixed(2) + "%";
      box.style.width = ((r - l) * 100).toFixed(2) + "%";
    };
    const onUp = (ev) => {
      if (startX == null) return;
      const cx = rect ? this._clamp((ev.clientX - rect.left) / rect.width, 0, 1) : startX;
      const f0 = Math.min(startX, cx), f1 = Math.max(startX, cx);
      if (box && box.parentNode) box.parentNode.removeChild(box);
      try { if (pid != null) host.releasePointerCapture(pid); } catch (e) { /* ignore */ }
      box = null; startX = null; pid = null;
      host.__dragging = false;
      if (f1 - f0 > 0.02) this._applyBrush(host, kind, f0, f1);
    };
    host.addEventListener("pointerdown", onDown);
    host.addEventListener("pointermove", onMove);
    host.addEventListener("pointerup", onUp);
    window.addEventListener("pointerup", onUp);
  }
  _updatePowerXaxis(z, startS, endS) {
    const ax = this._r.powerXaxis;
    if (!ax) return;
    if (!z) {
      ax.innerHTML = `<span>00</span><span>06</span><span>12</span><span>18</span><span>24</span>`;
      return;
    }
    const span = endS - startS;
    const t0 = startS + z.lo * span, t1 = startS + z.hi * span;
    ax.innerHTML = Array.from({ length: 5 }, (_, i) =>
      `<span>${this._fmtClock(t0 + ((t1 - t0) * i) / 4)}</span>`
    ).join("");
  }
  _updateMiniAxis(win) {
    const ax = this._r.miniAxis;
    if (!ax) return;
    ax.innerHTML = win
      ? `<span>${this._fmtClock(win.t0)}</span><span>${this._fmtClock(win.t1)}</span>`
      : `<span>00:00</span><span>${this._t("now")}</span>`;
  }

  // ----- Diagnostics body (section 2 of the SOC card, two columns) -----
  _buildDiagBody() {
    const wrap = document.createElement("div");
    wrap.className = "soc-diag";
    const title = document.createElement("div");
    title.className = "soc-diag-title";
    title.innerHTML = `<ha-icon icon="mdi:shield-check-outline"></ha-icon><span>${this._t("diagTitle")}</span>`;
    wrap.appendChild(title);

    const grid = document.createElement("div");
    grid.className = "diag-grid";
    this._r.diag = {};
    DIAG_ROWS.forEach((row) => {
      const cell = document.createElement("div");
      cell.className = "diag-cell";
      cell.innerHTML =
        `<span class="muted diag-cell-label">${this._t(row.lk)}</span>` +
        `<span class="chip diag-${row.key}">—</span>`;
      grid.appendChild(cell);
      this._r.diag[row.key] = cell.querySelector(".chip");
      // click a diagnostic -> more-info (history graph)
      this._linkMoreInfo(cell, this._sysEntityId(row.key));
    });
    wrap.appendChild(grid);
    return wrap;
  }

  /** Localized chip text + tone for one diagnostic entity. */
  _diagDisplay(key, so, m) {
    if (key === K.nonResponsive) {
      const off = m.offline || 0;
      return off > 0
        ? { text: this._t("nResponsive", { n: off }), tone: "warn" }
        : { text: this._t("none"), tone: "good" };
    }
    if (!so || so.state == null || so.state === "unknown" || so.state === "unavailable") {
      return { text: "—", tone: "neutral" };
    }
    const raw = String(so.state).toLowerCase();
    const disp =
      typeof this._hass.formatEntityState === "function"
        ? this._hass.formatEntityState(so)
        : so.state;
    switch (key) {
      case K.netBalance: {
        const n = this._num(so);
        if (n == null) return { text: "—", tone: "neutral" };
        return { text: `${n >= 0 ? "+" : ""}${this._nf(n, 2)} kWh`, tone: n >= 0 ? "good" : "warn" };
      }
      case K.sysAlarm:
        return {
          text: disp,
          tone: raw === "ok" ? "good" : raw === "warning" ? "warn" : raw === "fault" ? "bad" : "neutral",
        };
      case K.pdQuality:
        // stable=good, oscillating/sluggish=warn, battery_limited/collecting_data=neutral
        return {
          text: disp,
          tone: raw === "stable" ? "good" : (raw === "oscillating" || raw === "sluggish") ? "warn" : "neutral",
        };
      case K.predictiveActive:
      case K.curtailmentActive:
      case K.capacityActive:
        return { text: disp, tone: raw === "on" ? "good" : "neutral" };
      case K.dischargeWindow: {
        const n = so.attributes && so.attributes.active_slot;
        const txt = raw === "active" && n ? `${disp} · ${this._t("itemSlot")} ${n}` : disp;
        return { text: txt, tone: raw === "active" ? "good" : "neutral" };
      }
      case K.weeklyFullCharge:
        return { text: disp, tone: raw === "charging" || raw === "complete" ? "good" : "neutral" };
      case K.chargeDelay: {
        if (raw === "charging_allowed" || raw === "charging_to_setpoint") return { text: disp, tone: "good" };
        if (raw === "delayed" || raw === "waiting_for_solar") {
          // Append the estimated release time when known (attribute may be
          // empty while still "waiting_for_solar" before solar production starts).
          const until = so.attributes && so.attributes.estimated_unlock_time;
          return { text: until ? `${disp} · ${until}` : disp, tone: "warn" };
        }
        return { text: disp, tone: "neutral" };
      }
      case K.activeBatteries: {
        // State is "Discharging: <names>" / "Charging: <names>" / "Idle"; the
        // battery names also live in attributes. Show the active battery, not
        // just the direction word. Fall back to parsing the state if attrs miss.
        const a = so.attributes || {};
        const afterColon = String(so.state).split(":").slice(1).join(":").trim();
        if (raw.startsWith("discharging")) {
          const names = (a.discharge_batteries && a.discharge_batteries.join(", ")) || afterColon;
          return { text: names ? `${this._t("discharging")}: ${names}` : this._t("discharging"), tone: "good" };
        }
        if (raw.startsWith("charging")) {
          const names = (a.charge_batteries && a.charge_batteries.join(", ")) || afterColon;
          return { text: names ? `${this._t("charging")}: ${names}` : this._t("charging"), tone: "good" };
        }
        if (raw === "idle") return { text: this._t("idle"), tone: "neutral" };
        return { text: disp, tone: "neutral" };
      }
      case K.integration: {
        let tone = "good";
        if (raw.includes("blocked") || raw.includes("pause") || raw.includes("backup")) tone = "warn";
        else if (raw === "initializing") tone = "neutral";
        return { text: disp, tone };
      }
      case K.phaseProtection: {
        const limited = so.attributes && so.attributes.limited_batteries;
        const names = Array.isArray(limited) ? limited.join(", ") : "";
        if (raw === "limiting") {
          return { text: names ? `${disp}: ${names}` : disp, tone: "warn" };
        }
        if (raw === "degraded") return { text: disp, tone: "bad" };
        return { text: disp, tone: raw === "active" ? "good" : "neutral" };
      }
      default:
        return { text: disp, tone: "neutral" };
    }
  }

  // --- patch (data -> DOM) ---------------------------------------------------
  _setChip(el, text, tone) {
    if (!el) return;
    el.className = "chip chip-" + (tone || "neutral");
    el.textContent = text;
  }

  _patch(m) {
    const r = this._r;
    if (!r.flowSvg) return; // not on Resumen
    const p = (kw) => {
      const f = this._fmtPower(Math.abs(kw * 1000));
      return f.v + (f.u ? " " + f.u : "");
    };
    const off = (kw) => Math.abs(kw) > 0.03;

    // ----- flow nodes -----
    const { solar, home, grid, battery } = m;
    // solar
    const solActive = m.hasSolar && solar > 0.05;
    r.nSolar.node.style.display = m.hasSolar ? "" : "none";
    r.nSolar.node.classList.toggle("active", solActive);
    r.nSolar.val.textContent = m.hasSolar ? (solar > 0.03 ? p(solar) : "—") : "—";
    r.nSolar.unit.textContent = "";
    // grid
    const gridKnown = grid != null;
    const gridLabel = !gridKnown ? this._t("grid") : Math.abs(grid) < 0.03 ? this._t("grid") : grid > 0 ? this._t("importing") : this._t("exporting");
    r.nGrid.label.textContent = gridLabel;
    r.nGrid.node.classList.toggle("active", gridKnown && off(grid));
    r.nGrid.val.textContent = gridKnown ? p(grid) : "—";
    // home
    r.nHome.node.classList.toggle("active", home > 0.05);
    r.nHome.val.textContent = home != null ? p(home) : "—";
    // battery
    const battLabel = Math.abs(battery) < 0.03 ? this._t("idle") : battery > 0 ? this._t("charging") : this._t("discharging");
    r.nBatt.label.textContent = battLabel;
    r.nBatt.node.classList.toggle("active", off(battery));
    r.nBatt.val.textContent = p(battery);
    r.nBatt.badge.textContent =
      (m.soc != null ? Math.round(m.soc) : "—") + "% · " + m.active + " " + this._t("units");
    // excluded devices (summed power → into the car). Node hidden when no
    // excluded device exposes a power sensor.
    const exclActive = m.hasExcluded && m.excluded > 0.03;
    r.nExcl.node.style.display = m.hasExcluded ? "" : "none";
    r.nExcl.node.classList.toggle("active", exclActive);
    r.nExcl.val.textContent = m.hasExcluded ? (m.excluded > 0.03 ? p(m.excluded) : "—") : "—";

    // wires (animated node-graph) — skipped in scene mode
    if (r.wires.solar) {
      r.wires.solar.classList.toggle("on", solActive);
      r.wires.grid.classList.toggle("on", gridKnown && off(grid));
      r.wires.home.classList.toggle("on", home > 0.03);
      r.wires.batt.classList.toggle("on", off(battery));
    }

    // leader lines + element end-dots (scene mode)
    if (r.leads) {
      const lead = (edge, on) =>
        (r.leads[edge] || []).forEach((el) => el.classList.toggle("on", on));
      lead("solar", solActive);
      lead("grid", gridKnown && off(grid));
      lead("home", home > 0.05);
      lead("batt", off(battery));
      lead("excl", exclActive);
      (r.leads.solar || []).forEach((el) => (el.style.display = m.hasSolar ? "" : "none"));
      (r.leads.excl || []).forEach((el) => (el.style.display = m.hasExcluded ? "" : "none"));
    }

    // animated "snake" flow lines: color + travel direction follow the live state
    //   grid   → morado (import) / naranja (export, e.g. solar surplus)
    //   solar  → naranja
    //   batería→ verde (carga) / azul (descarga)
    // `rev` reverses the snake so it travels "into" the consuming node.
    if (r.flows) {
      const flow = (edge, on, color, rev) =>
        (r.flows[edge] || []).forEach((el) => {
          el.classList.toggle("on", on);
          el.classList.toggle("rev", !!rev);
          if (color) el.style.color = color; // stroke + glow inherit currentColor
        });
      flow("solar", solActive, "var(--solar)", false);
      flow(
        "grid",
        gridKnown && off(grid),
        grid > 0 ? "var(--flow-purple)" : "var(--flow-orange)",
        gridKnown && grid < 0
      );
      flow("home", home > 0.05, "var(--home)", true);
      flow(
        "batt",
        off(battery),
        battery > 0 ? "var(--flow-green)" : "var(--flow-blue)",
        battery < 0
      );
      // excluded loads always flow "into" the car (a consumer): rev=false sends
      // the snake toward the element attach point (the car), not the label.
      flow("excl", exclActive, "var(--home)", false);
      (r.flows.solar || []).forEach((el) => (el.style.display = m.hasSolar ? "" : "none"));
      (r.flows.excl || []).forEach((el) => (el.style.display = m.hasExcluded ? "" : "none"));
    }

    // day / night backdrop swap
    if (r.sceneImg) {
      const day = this._sceneDaytime(m);
      if (day !== this._sceneIsDay) {
        this._sceneIsDay = day;
        delete r.sceneImg.dataset.fb;
        r.sceneImg.src = day ? this._sceneDay : this._sceneNight;
      }
    }

    // particles
    this._patchEdge("solar", "mv-e-solar", "var(--solar)", solActive, false, solar);
    this._patchEdge("grid", "mv-e-grid", "var(--grid)", gridKnown && Math.abs(grid) > 0.05, gridKnown && grid < 0, grid || 0);
    this._patchEdge("home", "mv-e-home", "var(--home)", home > 0.05, true, home);
    this._patchEdge("batt", "mv-e-batt", "var(--battery)", Math.abs(battery) > 0.05, battery > 0, battery);

    // hub self-consumption
    const self = home > 0.03 ? this._clamp(100 * (1 - Math.max(0, grid || 0) / home), 0, 100) : 100;
    r.hubSelf.textContent = Math.round(self);

    // ----- SOC hero (ring colored by charge level) -----
    const socColor =
      m.soc == null ? "var(--battery)"
        : m.soc < 20 ? "oklch(0.7 0.18 25)"   // low — red
          : m.soc < 50 ? "oklch(0.82 0.14 75)"  // mid — amber
            : "var(--battery)";                    // healthy — accent
    if (m.soc != null) {
      r.ringFg.setAttribute(
        "stroke-dashoffset",
        (r.ringCirc * (1 - this._clamp(m.soc, 0, 100) / 100)).toFixed(2)
      );
      r.ringVal.innerHTML = Math.round(m.soc) + "<span>%</span>";
    }
    r.ringFg.setAttribute("stroke", socColor);
    r.ringFg.style.filter = `drop-shadow(0 0 8px ${socColor})`;
    r.ringSub.textContent = `${this._nf(m.stored, 2)} / ${this._nf(m.capacity, 2)} kWh`;

    // keep the SOC sparkline alive even if recorder history is empty: append the
    // live SOC (throttled to ~60 s, capped) so the line always renders
    if (m.soc != null) {
      const nowS = Date.now() / 1000;
      const v = this._clamp(m.soc, 0, 100);
      if (this._socSeries.length === 0) {
        this._socSeries.push(v, v);
        this._socLastPush = nowS;
        this._drawSpark();
      } else if (nowS - this._socLastPush > 60) {
        this._socSeries.push(v);
        if (this._socSeries.length > 240) this._socSeries.shift();
        this._socLastPush = nowS;
        this._drawSpark();
      }
    }

    // ----- system power (charge / discharge) + available headroom -----
    const ch = Math.max(0, battery) * 1000;
    const dis = Math.max(0, -battery) * 1000;
    const fc = this._fmtPower(ch), fd = this._fmtPower(dis);
    r.pwCharge.innerHTML = `${fc.v}<span class="stat-unit"> ${fc.u}</span>`;
    r.pwDisch.innerHTML = `${fd.v}<span class="stat-unit"> ${fd.u}</span>`;
    let tcap = battery >= 0 ? m.maxCharge : m.maxDischarge;
    if (!tcap) tcap = 2500 * Math.max(1, m.active);
    r.pwBar.style.width = this._clamp((Math.abs(battery) * 1000 / tcap) * 100, 0, 100) + "%";
    r.pwBar.style.background = battery >= 0 ? "var(--battery)" : "var(--grid)";
    const ftc = this._fmtPower(tcap);
    r.pwAvail.textContent = this._t("availOf", { value: `${ftc.v} ${ftc.u}` });

    // ----- daily energy -----
    const sol = m.dailySolar;
    const hm = m.dailyHome;
    const imp = m.dailyGridImport;
    const exp = m.dailyGridExport;
    const forecast = m.forecastInitial;
    const remaining = m.solarRemaining;
    const expected = m.expectedConsumption;
    const u = `<span class="dim" style="font-size:11px"> kWh</span>`;
    const max = Math.max(
      m.dailyCharge || 0, m.dailyDischarge || 0, sol || 0, hm || 0,
      imp || 0, exp || 0, forecast || 0, remaining || 0, expected || 0, 0.1
    );
    r.dChV.innerHTML = `${this._nf(m.dailyCharge, 2)}${u}`;
    r.dChBar.style.width = ((m.dailyCharge || 0) / max) * 100 + "%";
    r.dDisV.innerHTML = `${this._nf(m.dailyDischarge, 2)}${u}`;
    r.dDisBar.style.width = ((m.dailyDischarge || 0) / max) * 100 + "%";
    // solar / home rows hide entirely when no source sensor is configured
    if (r.dSolRow) r.dSolRow.style.display = sol == null ? "none" : "";
    if (sol != null) {
      r.dSolV.innerHTML = `${this._nf(sol, 2)}${u}`;
      r.dSolBar.style.width = (sol / max) * 100 + "%";
    }
    if (r.dHomeRow) r.dHomeRow.style.display = hm == null ? "none" : "";
    if (hm != null) {
      r.dHomeV.innerHTML = `${this._nf(hm, 2)}${u}`;
      r.dHomeBar.style.width = (hm / max) * 100 + "%";
    }
    // grid import / export — hidden until the integrated history is available
    if (r.dImpRow) r.dImpRow.style.display = imp == null ? "none" : "";
    if (imp != null) {
      r.dImpV.innerHTML = `${this._nf(imp, 2)}${u}`;
      r.dImpBar.style.width = (imp / max) * 100 + "%";
    }
    if (r.dExpRow) r.dExpRow.style.display = exp == null ? "none" : "";
    if (exp != null) {
      r.dExpV.innerHTML = `${this._nf(exp, 2)}${u}`;
      r.dExpBar.style.width = (exp / max) * 100 + "%";
    }

    // ----- daily forecasts -----
    if (r.dForecastRow) r.dForecastRow.style.display = forecast == null ? "none" : "";
    if (forecast != null) {
      r.dForecastV.innerHTML = `${this._nf(forecast, 2)}${u}`;
      r.dForecastBar.style.width = (forecast / max) * 100 + "%";
    }
    if (r.dRemainingRow) r.dRemainingRow.style.display = remaining == null ? "none" : "";
    if (remaining != null) {
      r.dRemainingV.innerHTML = `${this._nf(remaining, 2)}${u}`;
      r.dRemainingBar.style.width = (remaining / max) * 100 + "%";
    }
    if (r.dExpectedRow) r.dExpectedRow.style.display = expected == null ? "none" : "";
    if (expected != null) {
      r.dExpectedV.innerHTML = `${this._nf(expected, 2)}${u}`;
      r.dExpectedBar.style.width = (expected / max) * 100 + "%";
    }

    // ----- diagnostics (section 2, two columns) -----
    const ds = m.diagStates || {};
    for (const row of DIAG_ROWS) {
      const el = r.diag[row.key];
      if (!el) continue;
      const { text, tone } = this._diagDisplay(row.key, ds[row.key], m);
      this._setChip(el, text, tone);
      el.title = `${this._t(row.lk)}: ${text}`; // full value on hover (chips ellipsize)
    }

    this._patchDailyOperationTimeline();
  }

  // --- history (SOC sparkline + Potencias + Energía semanal) -----------------
  _startHistory() {
    this._refreshHistory();
    if (this._histTimer) clearInterval(this._histTimer);
    this._histTimer = setInterval(() => this._refreshHistory(), 5 * 60 * 1000);
  }

  /** Start recorder queries only after the Resumen cards exist. HA can inject
   *  `hass` before connecting this element, in which case a fast response used
   *  to draw into no plot and left Potencias empty until a later UI repaint. */
  _ensureHistoryStarted() {
    if (this._historyStarted || !this._hass || !this._built || !this.isConnected) return;
    this._historyStarted = true;
    this._startHistory();
  }

  _refreshHistory() {
    // A slow recorder must not accumulate another three queries every five
    // minutes.  Keep one refresh in flight and prioritize the two visible
    // charts; the auxiliary SOC sparkline can follow afterwards.
    if (this._historyRefresh) return this._historyRefresh;
    this._historyRefresh = (async () => {
      await Promise.all([this._fetchPowerHistory(), this._fetchWeeklyEnergy()]);
      await this._fetchHistory();
    })().finally(() => { this._historyRefresh = null; });
    return this._historyRefresh;
  }

  async _fetchHistory() {
    if (!this._hass || !this._hass.callWS) return;
    // resolve a SOC entity: prefer system, else first battery SOC
    const { byKey } = this._index();
    const sysSoc = (byKey.get(K.sysSoc) || [])[0];
    const battSoc = (byKey.get(K.batterySoc) || [])[0];
    const socId = sysSoc || battSoc;
    if (!socId) return;
    const { grid, startISO } = this._historyGrid();
    try {
      const res = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: startISO,
        end_time: new Date().toISOString(),
        entity_ids: [socId],
        minimal_response: true,
        no_attributes: true,
      });
      const arr = res && res[socId];
      if (!Array.isArray(arr) || !arr.length) return;
      // Parse (timestamp, %) pairs and step-hold sample onto the uniform
      // midnight→now grid, so the sparkline's index→clock mapping is real time.
      // minimal_response returns one entry per state CHANGE, so a flat SOC
      // plateau yields few samples; plotting those by index alone would compress
      // the plateau to the right edge and make a hours-old peak look like "now".
      const pts = [];
      for (const it of arr) {
        const v = Number(it.s != null ? it.s : it.state);
        const t =
          it.lu != null ? it.lu
            : it.last_updated ? Date.parse(it.last_updated) / 1000
              : it.last_changed ? Date.parse(it.last_changed) / 1000
                : null;
        if (t == null || Number.isNaN(v)) continue;
        pts.push([t, v]);
      }
      if (!pts.length) return;
      pts.sort((a, b) => a[0] - b[0]);
      const nowParts = this._dateParts();
      const localDate = `${nowParts.year}-${String(nowParts.month).padStart(2, "0")}-${String(nowParts.day).padStart(2, "0")}`;
      const changes = new Map();
      let heldSoc = null;
      for (const [timestamp, value] of pts) {
        const parts = this._dateParts(timestamp * 1000);
        const pointDate = `${parts.year}-${String(parts.month).padStart(2, "0")}-${String(parts.day).padStart(2, "0")}`;
        if (pointDate < localDate) {
          heldSoc = value;
          continue;
        }
        if (pointDate !== localDate) continue;
        changes.set(parts.hour * 4 + Math.floor(parts.minute / 15), value);
      }
      const history = Array(96).fill(null);
      const currentIndex = nowParts.hour * 4 + Math.floor(nowParts.minute / 15);
      for (let index = 0; index <= currentIndex; index++) {
        if (changes.has(index)) heldSoc = changes.get(index);
        if (heldSoc != null) history[index] = heldSoc;
      }
      this._dailyOperationSocHistory = history;
      this._dailyOperationSocHistoryDate = localDate;
      const series = [];
      let j = 0, cur = pts[0][1];
      for (const gt of grid) {
        while (j < pts.length && pts[j][0] <= gt) { cur = pts[j][1]; j++; }
        series.push(cur);
      }
      this._socSeries = series;
      this._socLastPush = Date.now() / 1000;
      this._drawSpark();
      this._patchDailyOperationTimeline();
    } catch (e) {
      console.debug("[mvem] SOC history fetch failed", e);
    }
  }

  /** Build a step-hold sampler grid from local midnight to now (N+1 points). */
  _historyGrid(n = 144) {
    const startMs = this._dayStartEpoch();
    const startS = startMs / 1000;
    const nowS = Date.now() / 1000;
    const grid = [];
    for (let i = 0; i <= n; i++) grid.push(startS + (nowS - startS) * (i / n));
    return { grid, startISO: new Date(startMs).toISOString() };
  }

  /** Resample one entity's recorder history onto `grid` (step-hold), in kW. */
  _sampleToGrid(res, id, grid) {
    const arr = res && res[id];
    if (!Array.isArray(arr) || !arr.length) return null;
    const so = this._hass.states[id];
    const unit = ((so && so.attributes.unit_of_measurement) || "").toLowerCase();
    const toKw = unit === "kw" ? 1 : 0.001;
    const pts = [];
    for (const it of arr) {
      const v = Number(it.s != null ? it.s : it.state);
      const t =
        it.lu != null ? it.lu
          : it.last_updated ? Date.parse(it.last_updated) / 1000
            : it.last_changed ? Date.parse(it.last_changed) / 1000
              : null;
      if (t == null || Number.isNaN(v)) continue;
      pts.push([t, v * toKw]);
    }
    if (!pts.length) return null;
    pts.sort((a, b) => a[0] - b[0]);
    const out = [];
    let j = 0, cur = null;
    for (const gt of grid) {
      while (j < pts.length && pts[j][0] <= gt) { cur = pts[j][1]; j++; }
      out.push(cur);
    }
    return out;
  }

  /** Resample Recorder statistics onto a grid.  Statistics rows are already
   *  condensed by Home Assistant, so using them avoids shipping every raw
   *  state transition to the browser. */
  _sampleStatisticsToGrid(res, id, grid, field = "mean") {
    const arr = res && res[id];
    if (!Array.isArray(arr) || !arr.length) return null;
    const so = this._hass.states[id];
    const unit = ((so && so.attributes.unit_of_measurement) || "").toLowerCase();
    const toKw = unit === "kw" ? 1 : 0.001;
    const pts = [];
    for (const it of arr) {
      const v = Number(it[field]);
      // Statistics WebSocket timestamps are milliseconds since epoch.  Accept
      // seconds too, so the panel remains tolerant of older HA responses.
      const rawT = Number(it.start);
      const t = rawT > 1e11 ? rawT / 1000 : rawT;
      if (!Number.isFinite(t) || Number.isNaN(v)) continue;
      pts.push([t, v * toKw]);
    }
    if (!pts.length) return null;
    pts.sort((a, b) => a[0] - b[0]);
    const out = [];
    let j = 0, cur = null;
    for (const gt of grid) {
      while (j < pts.length && pts[j][0] <= gt) { cur = pts[j][1]; j++; }
      out.push(cur);
    }
    return out;
  }

  /** Fetch a bounded Recorder statistics series.  The caller falls back to
   *  raw history only for entities which do not publish statistics. */
  async _fetchStatistics(ids, startISO, endISO, period, types) {
    if (!ids.length || !this._hass || !this._hass.callWS) return null;
    try {
      return await this._hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: startISO,
        end_time: endISO,
        statistic_ids: ids,
        period,
        types,
      });
    } catch (e) {
      // Some externally configured sensors have no statistics metadata.  The
      // regular history endpoint below preserves their charts.
      console.debug("[mvem] statistics fetch failed; using history fallback", e);
      return null;
    }
  }

  /** 24 h power history for the Potencias chart (Solar/Casa/Batería/Red, kW). */
  async _fetchPowerHistory() {
    if (!this._hass || !this._hass.callWS) return;
    const cfg = this._panelConfig;
    const { byKey } = this._index();
    const sysCh = (byKey.get(K.sysChargePower) || [])[0];
    const sysDis = (byKey.get(K.sysDischargePower) || [])[0];
    const acIds = byKey.get(K.acPower) || [];
    const ids = new Set();
    const homeEid = this._homeEntityId(this._hass);
    if (cfg.solar_entity) ids.add(cfg.solar_entity);
    if (homeEid) ids.add(homeEid);
    if (cfg.grid_entity) ids.add(cfg.grid_entity);
    // Query the system charge/discharge aggregates AND the per-battery AC power.
    // The system sensors are preferred, but they can be `unavailable` (e.g. a
    // single-battery setup where the aggregate stays down); in that case we fall
    // back to per-battery ac_power so the Batería line still renders.
    if (sysCh) ids.add(sysCh);
    if (sysDis) ids.add(sysDis);
    acIds.forEach((x) => x && ids.add(x));
    if (!ids.size) { this._powerSeries = null; this._drawPowerHistory(); return; }
    const { grid, startISO } = this._historyGrid();
    const idList = [...ids];
    const endISO = new Date().toISOString();
    const statistics = await this._fetchStatistics(idList, startISO, endISO, "5minute", ["mean"]);
    const hasStatistics = (id) => Array.isArray(statistics && statistics[id]) && statistics[id].length;
    const fallbackIds = idList.filter((id) => !hasStatistics(id));
    let history = null;
    if (fallbackIds.length) {
      try {
        history = await this._hass.callWS({
          type: "history/history_during_period",
          start_time: startISO,
          end_time: endISO,
          entity_ids: fallbackIds,
          minimal_response: true,
          no_attributes: true,
        });
      } catch (e) {
        console.debug("[mvem] power history fallback failed", e);
      }
    }
    const sample = (id) =>
      this._sampleStatisticsToGrid(statistics, id, grid) || this._sampleToGrid(history, id, grid);
    const solar = cfg.solar_entity ? sample(cfg.solar_entity) : null;
    const home = homeEid ? sample(homeEid) : null;
    let gridS = cfg.grid_entity ? sample(cfg.grid_entity) : null;
    // Match the integration's +import / -export convention for an inverted meter.
    if (gridS && cfg.grid_inverted) gridS = gridS.map((v) => (v == null ? v : -v));
    let battery = null;
    if (sysCh || sysDis) {
      const ch = sysCh ? sample(sysCh) : null;
      const di = sysDis ? sample(sysDis) : null;
      if (ch || di) battery = grid.map((_, i) => ((ch && ch[i]) || 0) - ((di && di[i]) || 0));
    }
    // Fall back to per-battery ac_power when the system aggregate has no history
    // (sign in ac_power is - charge / + discharge, so negate to + charge / - discharge).
    if (battery == null && acIds.length) {
      const samples = acIds.map(sample).filter(Boolean);
      if (samples.length) battery = grid.map((_, i) => -samples.reduce((a, s) => a + (s[i] || 0), 0));
    }
    this._powerSeries = { t: grid, solar, home, grid: gridS, battery };
    this._drawPowerHistory();
  }

  /** Last 7 days of daily charge/discharge for the Energía semanal bars (kWh).
   *  Daily sensors are total_increasing that reset at local midnight, so the
   *  per-day max equals that day's total; sum across batteries when no system
   *  aggregate exists. */
  async _fetchWeeklyEnergy() {
    if (!this._hass || !this._hass.callWS) return;
    const { byKey } = this._index();
    const chSys = (byKey.get(K.sysDailyCharge) || [])[0];
    const diSys = (byKey.get(K.sysDailyDischarge) || [])[0];
    const chIds = chSys ? [chSys] : (byKey.get(K.dailyCharge) || []);
    const diIds = diSys ? [diSys] : (byKey.get(K.dailyDischarge) || []);
    if (!chIds.length && !diIds.length) { this._weekly = null; this._drawWeekly(); return; }
    const impSys = (byKey.get(K.sysDailyGridImport) || [])[0];
    const expSys = (byKey.get(K.sysDailyGridExport) || [])[0];
    const impIds = impSys ? [impSys] : [];
    const expIds = expSys ? [expSys] : [];
    const days = 7;
    const boundaries = Array.from(
      { length: days + 1 }, (_, k) => this._dayStartEpoch(k - (days - 1))
    );
    const allIds = [...new Set([...chIds, ...diIds, ...impIds, ...expIds])];
    const startISO = new Date(boundaries[0]).toISOString();
    const endISO = new Date().toISOString();
    // These sources reset at midnight.  Their Recorder ``sum`` is a
    // lifetime-normalized value, so its daily ``change`` can include reset
    // corrections and wildly overstate the day's energy.  The final daily
    // state is the counter's true total, matching the former history maximum.
    const statistics = await this._fetchStatistics(allIds, startISO, endISO, "day", ["state"]);
    const hasStatistics = (id) =>
      Array.isArray(statistics && statistics[id]) &&
      statistics[id].some((row) => Number.isFinite(Number(row.state)));
    const fallbackIds = allIds.filter((id) => !hasStatistics(id));
    let history = null;
    if (fallbackIds.length) {
      try {
        history = await this._hass.callWS({
          type: "history/history_during_period",
          start_time: startISO,
          end_time: endISO,
          entity_ids: fallbackIds,
          minimal_response: true,
          no_attributes: true,
        });
      } catch (e) {
        console.debug("[mvem] weekly history fallback failed", e);
      }
    }
    if (!statistics && !history) return;
    const dayIndex = (ms) => boundaries.findIndex(
      (start, k) => k < days && ms >= start && ms < boundaries[k + 1]
    );
    // Prefer each entity's final daily statistics state, then sum across ids.
    // Raw history fallbacks retain the former daily-maximum calculation.
    const dailyTotals = (entIds) => {
      const total = new Array(days).fill(null);
      for (const id of entIds) {
        const perDay = new Array(days).fill(null);
        const statRows = statistics && statistics[id];
        if (Array.isArray(statRows) && statRows.length) {
          for (const it of statRows) {
            const v = Number(it.state);
            const t = Number(it.start);
            if (!Number.isFinite(t) || Number.isNaN(v)) continue;
            const k = dayIndex(t);
            if (k >= 0 && k < days) perDay[k] = v;
          }
        } else {
          const arr = history && history[id];
          if (!Array.isArray(arr)) continue;
          for (const it of arr) {
            const v = Number(it.s != null ? it.s : it.state);
            const t =
              it.lu != null ? it.lu * 1000
                : it.last_updated ? Date.parse(it.last_updated)
                  : it.last_changed ? Date.parse(it.last_changed)
                    : null;
            if (t == null || Number.isNaN(v)) continue;
            const k = dayIndex(t);
            if (k < 0 || k >= days) continue;
            if (perDay[k] == null || v > perDay[k]) perDay[k] = v;
          }
        }
        for (let k = 0; k < days; k++) if (perDay[k] != null) total[k] = (total[k] || 0) + perDay[k];
      }
      return total;
    };
    const charge = dailyTotals(chIds);
    const discharge = dailyTotals(diIds);
    // grid import/export: per-day total = daily-reset sensor's max for that day
    const impTot = impIds.length ? dailyTotals(impIds) : null;
    const expTot = expIds.length ? dailyTotals(expIds) : null;

    // The current day has an authoritative live value in hass.states. Prefer it
    // over the historical maximum: a transient bad reading stored earlier today
    // must not make the weekly bar disagree with the Energía hoy card.
    const liveTotal = (entIds) => {
      let total = 0;
      let hasData = false;
      for (const id of entIds) {
        const value = this._num(this._hass.states[id]);
        if (value != null) {
          total += value;
          hasData = true;
        }
      }
      return hasData ? total : null;
    };
    const todayIndex = days - 1;
    const liveCharge = liveTotal(chIds);
    const liveDischarge = liveTotal(diIds);
    const liveImport = liveTotal(impIds);
    const liveExport = liveTotal(expIds);
    if (liveCharge != null) charge[todayIndex] = liveCharge;
    if (liveDischarge != null) discharge[todayIndex] = liveDischarge;
    if (impTot && liveImport != null) impTot[todayIndex] = liveImport;
    if (expTot && liveExport != null) expTot[todayIndex] = liveExport;
    const labels = [];
    for (let k = 0; k < days; k++) {
      const dd = new Date(boundaries[k]);
      labels.push(dd.toLocaleDateString(
        this._lang(), this._dateTimeOptions({ weekday: "short" })
      ));
    }
    this._weekly = {
      days: labels,
      charge: charge.map((v) => v || 0),
      discharge: discharge.map((v) => v || 0),
      import: impTot ? impTot.map((v) => v || 0) : null,
      export: expTot ? expTot.map((v) => v || 0) : null,
    };
    this._drawWeekly();
  }

  // ===== Baterías view =======================================================
  /** SOC-tiered ring color: <20 red, <50 amber, else accent. */
  _socColor(soc) {
    if (soc == null) return "var(--battery)";
    if (soc < 20) return "oklch(0.7 0.18 25)";
    if (soc < 50) return "oklch(0.82 0.14 75)";
    return "var(--battery)";
  }
  /** Trimmed string state, or null when empty/unknown/unavailable. */
  _sval(so) {
    if (!so || so.state == null) return null;
    const s = String(so.state).trim();
    if (!s || s === "unknown" || s === "unavailable") return null;
    return s;
  }
  /** "123 W" / "1.20 kW" as a single string. */
  _fmtPowerStr(w) {
    const f = this._fmtPower(w);
    return f.v + (f.u ? " " + f.u : "");
  }

  /** One model object per battery device (has a battery_soc entity). */
  _batteryModel() {
    const { byDevice } = this._index();
    const hass = this._hass;
    const list = [];
    for (const [dev, ids] of byDevice) {
      const byTk = {};
      // translation_key -> entity_id (last wins; used for sensors/metrics).
      // Controls also need domain-qualified lookup so Anker's read-only
      // max_charge_power sensor is not mistaken for the soft-max number.
      const idByTk = {};
      const idByTkDomain = {}; // `${domain}:${translation_key}` -> entity_id
      for (const id of ids) {
        const e = hass.entities[id];
        if (e && e.translation_key) {
          byTk[e.translation_key] = hass.states[id];
          idByTk[e.translation_key] = id;
          const domain = id.split(".")[0];
          idByTkDomain[`${domain}:${e.translation_key}`] = id;
        }
      }
      const socObj = byTk[K.batterySoc];
      if (!socObj) continue; // not a battery device
      const acW = this._watts(byTk[K.acPower]);
      const offgridW = this._watts(byTk[K.acOffgridPower]);
      const cmax = this._num(byTk[K.cellMax]);
      const cmin = this._num(byTk[K.cellMin]);
      const mppt = MPPT_KEYS.map((k) => this._num(byTk[k]));
      const hasMppt = mppt.some((v) => v != null);
      const mpptTotalW = hasMppt ? mppt.reduce((sum, value) => sum + (value || 0), 0) : null;
      const inverter = byTk[K.inverterState] || null;
      const invBackup = /backup/i.test(this._sval(inverter) || "");
      // Venus A/D can feed the AC bus while DC-coupled PV charges the cells.
      // Show the real cell balance as the battery flow, while retaining acW as a
      // separate AC-port flow: cell = MPPT - AC output - active backup output.
      const powerW =
        hasMppt && acW != null
          ? -acW - (invBackup ? offgridW || 0 : 0) + (mpptTotalW || 0)
          : acW != null
            ? -acW
            : this._watts(byTk[K.batteryPower]);
      const devReg = (hass.devices && hass.devices[dev]) || null;
      const name =
        (devReg && (devReg.name_by_user || devReg.name)) ||
        this._sval(byTk[K.deviceName]) ||
        null;
      list.push({
        dev,
        name,
        // model label rides on the battery_soc entity attributes (device-registry
        // model is hardcoded "Venus"): Marstek version / Zendure product.
        model: (socObj.attributes && socObj.attributes.model) || (devReg && devReg.model) || null,
        soc: this._num(socObj),
        // Net cell flow (+charge / -discharge). On Venus A/D this includes MPPT;
        // on AC-only units it remains the inverse of ac_power.
        powerW,
        // AC-port convention: +output to home/grid, -input from the AC bus.
        acFlowW: acW,
        mpptTotalW,
        offgridW,
        backupOn: (byTk[K.backupFunction] || {}).state === "on",
        hysteresisActive: (() => {
          const s = byTk[K.chargeHysteresisActive];
          return s ? (s.state === "on" ? true : s.state === "off" ? false : null) : null;
        })(),
        stored: this._num(byTk[K.storedEnergy]),
        capacity: this._num(byTk[K.batteryTotalEnergy]),
        inverter,
        temp: this._num(byTk[K.internalTemp]),
        voltage: this._num(byTk[K.batteryVoltage]),
        cellMax: cmax,
        cellMin: cmin,
        // measured delta (mV) from the cell_delta balance sensor — NOT the live
        // max-min, which swings with load. null until the first balance reading.
        cellDelta: this._num(byTk[K.cellDelta]),
        cycles: this._num(byTk[K.cycles]),
        cyclesCalc: this._num(byTk[K.cyclesCalc]),
        rte: this._num(byTk[K.rte]),
        dailyCharge: this._num(byTk[K.dailyCharge]),
        dailyDischarge: this._num(byTk[K.dailyDischarge]),
        maxCharge: this._num(byTk[K.maxChargePower]),
        maxDischarge: this._num(byTk[K.maxDischargePower]),
        mppt,
        hasMppt,
        entIds: idByTk,
        entIdsDomain: idByTkDomain,
        info: {
          manufacturer: (socObj.attributes && socObj.attributes.manufacturer) || (devReg && devReg.manufacturer) || null,
          sw: this._sval(byTk[K.softwareVersion]),
          sunspecModelType: (socObj.attributes && socObj.attributes.sunspec_model_type) || this._sval(byTk[K.froniusSunspecModelType]),
          // Huawei publishes the serial as a sensor; the registry entry has none.
          serial: (socObj.attributes && socObj.attributes.serial) || (devReg && devReg.serial_number) || this._sval(byTk[K.powerModuleSerial]),
          powerModuleFw: this._sval(byTk[K.powerModuleFirmware]),
          inverterModel: this._sval(byTk[K.deviceName]),
          inverterSn: this._sval(byTk[K.inverterSerial]),
          inverterFw: this._sval(byTk[K.inverterFirmware]),
          // A battery built from packs names each one. Empty slots answer with
          // nothing and are left out rather than listed blank.
          packs: [1, 2, 3]
            .map((n) => ({
              n,
              fw: this._sval(byTk[K["pack" + n + "Firmware"]]),
              sn: this._sval(byTk[K["pack" + n + "Serial"]]),
            }))
            .filter((pack) => pack.fw || pack.sn),
          bms: this._sval(byTk[K.bmsVersion]),
          vms: this._sval(byTk[K.vmsVersion]),
          ems: this._sval(byTk[K.emsVersion]),
          comm: this._sval(byTk[K.commFw]),
          wifiSignal: this._num(byTk[K.wifiSignal]),
          wifiStatus: byTk[K.wifiStatus] || null,
          mac: this._sval(byTk[K.mac]),
        },
      });
    }
    list.sort((a, b) =>
      String(a.name || a.dev).localeCompare(String(b.name || b.dev), this._lang())
    );
    return list;
  }

  _renderBaterias() {
    this._batCards = {};
    const list = this._batteryModel();
    this._batSig = list.map((b) => b.dev).sort().join("|");
    const wrap = document.createElement("div");
    wrap.className = "bat-grid";
    if (!list.length) {
      const e = document.createElement("div");
      e.className = "placeholder";
      e.innerHTML =
        `<ha-icon icon="mdi:battery-off-outline"></ha-icon><h3>${this._t("noBatteriesTitle")}</h3>` +
        `<p>${this._t("noBatteriesMsg")}</p>`;
      wrap.appendChild(e);
      return wrap;
    }
    for (const b of list) wrap.appendChild(this._buildBatteryCard(b));
    return wrap;
  }

  /** Small SOC ring (DOM built once, animated via stroke-dashoffset on patch). */
  _buildBatRing() {
    const size = 116, stroke = 11, pad = 6;
    const r = (size - stroke) / 2 - pad;
    const circ = 2 * Math.PI * r;
    const ring = document.createElement("div");
    ring.className = "ring bat-ring";
    ring.style.width = size + "px";
    ring.style.height = size + "px";
    ring.innerHTML = `
      <svg width="${size}" height="${size}" style="transform:rotate(-90deg)">
        <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--bg-2)" stroke-width="${stroke}"/>
        <circle class="ring-fg" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--battery)"
          stroke-width="${stroke}" stroke-linecap="round"
          stroke-dasharray="${circ.toFixed(2)}" stroke-dashoffset="${circ.toFixed(2)}"/>
      </svg>
      <div class="ring-center"><div class="num ring-val">—<span>%</span></div></div>`;
    return { ring, fg: ring.querySelector(".ring-fg"), circ, val: ring.querySelector(".ring-val") };
  }

  _buildBatteryCard(b) {
    const card = document.createElement("div");
    card.className = "card bat-card";

    const head = document.createElement("div");
    head.className = "bat-head";
    head.innerHTML =
      `<div class="bat-title"><span class="ic"><ha-icon icon="mdi:battery-high"></ha-icon></span>` +
      `<span class="bat-name"></span></div>` +
      `<div class="bat-chips"><span class="chip bat-state">—</span></div>`;
    card.appendChild(head);

    // ----- top: SOC ring + power readout -----
    const top = document.createElement("div");
    top.className = "bat-top";
    const ring = this._buildBatRing();
    const pw = document.createElement("div");
    pw.className = "bat-power";
    pw.innerHTML =
      `<div class="bat-standard-flow">` +
      `<div class="bat-pwr"><span class="num bat-pwr-val">—</span><span class="bat-pwr-unit dim"></span></div>` +
      `<div class="muted bat-pwr-lbl">—</div></div>` +
      `<div class="bat-mppt-flows" style="display:none">` +
      `<div class="bat-flow bat-ac-flow">` +
      `<div class="muted bat-flow-label bat-ac-title">${this._t("acOutput")}</div>` +
      `<div><span class="num bat-flow-value bat-ac-val">—</span><span class="dim bat-flow-unit bat-ac-unit"></span></div>` +
      `<div class="muted bat-flow-sub bat-ac-sub">—</div></div>` +
      `<div class="bat-flow bat-cell-flow">` +
      `<div class="muted bat-flow-label">${this._t("battery")}</div>` +
      `<div><span class="num bat-flow-value bat-cell-val">—</span><span class="dim bat-flow-unit bat-cell-unit"></span></div>` +
      `<div class="muted bat-flow-sub bat-cell-sub">—</div></div>` +
      `<div class="bat-mppt-total"><span class="muted">${this._t("solarMppt")}</span><span class="num bat-mppt-total-val">—</span></div>` +
      `</div>` +
      `<div class="socbar bat-pwr-track" style="height:6px;margin-top:8px"><span class="bat-pwr-bar"></span></div>` +
      `<div class="dim bat-pwr-avail">—</div>` +
      `<div class="dim bat-cap">— / — kWh</div>` +
      // off-grid power, pinned to the right edge at the AC-power line; shown only
      // when the backup function switch is on (see _patchBatteryCard).
      `<div class="bat-offgrid" style="display:none">` +
      `<div class="bat-pwr"><span class="num bat-og-val">—</span><span class="bat-og-unit dim"></span></div>` +
      `<div class="muted bat-og-lbl">${this._t("offgrid")}</div>` +
      `</div>`;
    top.appendChild(ring.ring);
    top.appendChild(pw);
    // click SOC ring / power / capacity -> more-info (history graph)
    this._linkMoreInfo(ring.ring, b.entIds[K.batterySoc]);
    // AC-only models retain their single power readout. Venus A/D links each
    // side of the dual readout to its matching AC/cell/solar history sensor.
    this._linkMoreInfo(pw.querySelector(".bat-pwr"), b.entIds[K.acPower] || b.entIds[K.batteryPower]);
    this._linkMoreInfo(pw.querySelector(".bat-ac-flow"), b.entIds[K.acPower]);
    this._linkMoreInfo(pw.querySelector(".bat-cell-flow"), b.entIds[K.batteryCellPower]);
    this._linkMoreInfo(pw.querySelector(".bat-mppt-total"), b.entIds[K.solarPower]);
    this._linkMoreInfo(pw.querySelector(".bat-cap"), b.entIds[K.storedEnergy]);
    card.appendChild(top);

    // ----- salud y celdas -----
    const health = document.createElement("div");
    health.className = "bat-sect";
    health.innerHTML = `<div class="bat-sect-t">${this._t("healthCells")}</div>`;
    const hgrid = document.createElement("div");
    hgrid.className = "bat-metrics";
    const M = {};
    const addMetric = (id, label, tk) => {
      const c = document.createElement("div");
      c.className = "metric";
      c.innerHTML = `<span class="m-k muted">${label}</span><span class="m-v num">—</span>`;
      // click the metric -> open HA's more-info dialog (shows the history graph)
      if (tk) this._linkMoreInfo(c, b.entIds[tk]);
      hgrid.appendChild(c);
      M[id] = c.querySelector(".m-v");
    };
    addMetric("temp", this._t("mTemp"), K.internalTemp);
    addMetric("volt", this._t("mVoltage"), K.batteryVoltage);
    addMetric("cmax", this._t("mCellMax"), K.cellMax);
    addMetric("cmin", this._t("mCellMin"), K.cellMin);
    addMetric("cdelta", this._t("mCellDelta"), K.cellDelta);
    addMetric("cycles", this._t("mCycles"), b.entIds[K.cycles] ? K.cycles : K.cyclesCalc);
    addMetric("rte", this._t("mEfficiency"), K.rte);
    addMetric("hyst", this._t("mHysteresis"), K.chargeHysteresisActive); // col2 row4: right of Efficiency, below Cycles
    health.appendChild(hgrid);
    card.appendChild(health);

    // ----- energía hoy -----
    const en = document.createElement("div");
    en.className = "bat-sect";
    en.innerHTML = `<div class="bat-sect-t">${this._t("cardDaily")}</div>`;
    const ebody = document.createElement("div");
    ebody.className = "daily-body";
    const ebar = (cls, label, color) => `
      <div class="daily-row">
        <div class="daily-head"><span class="muted">${label}</span>
          <span class="num bat-${cls}-v">—<span class="dim" style="font-size:11px"> kWh</span></span></div>
        <div class="socbar"><span class="bat-${cls}-bar" style="background:${color}"></span></div>
      </div>`;
    ebody.innerHTML =
      ebar("ch", this._t("charged"), "var(--battery)") + ebar("dis", this._t("discharged"), "var(--grid)");
    const dRows = ebody.querySelectorAll(".daily-row");
    this._linkMoreInfo(dRows[0], b.entIds[K.dailyCharge]);
    this._linkMoreInfo(dRows[1], b.entIds[K.dailyDischarge]);
    en.appendChild(ebody);
    card.appendChild(en);

    // ----- solar (MPPT) — hidden when the model exposes none -----
    const mppt = document.createElement("div");
    mppt.className = "bat-sect bat-mppt";
    mppt.innerHTML = `<div class="bat-sect-t">${this._t("solarMppt")}</div><div class="bat-mppt-chips"></div>`;
    card.appendChild(mppt);

    // ----- controles (collapsible) -----
    const controls = document.createElement("details");
    controls.className = "bat-info bat-controls";
    controls.innerHTML =
      `<summary><ha-icon icon="mdi:tune-variant"></ha-icon>${this._t("controls")}</summary>` +
      `<div class="bat-ctl-grid"></div>`;
    card.appendChild(controls);

    // ----- info (collapsible) -----
    const info = document.createElement("details");
    info.className = "bat-info";
    info.innerHTML = `<summary><ha-icon icon="mdi:information-outline"></ha-icon>${this._t("deviceInfo")}</summary><div class="bat-info-grid"></div>`;
    card.appendChild(info);

    this._batCards[b.dev] = {
      card,
      name: head.querySelector(".bat-name"),
      state: head.querySelector(".bat-state"),
      ringFg: ring.fg,
      ringCirc: ring.circ,
      ringVal: ring.val,
      powerRoot: pw,
      standardFlow: pw.querySelector(".bat-standard-flow"),
      mpptFlows: pw.querySelector(".bat-mppt-flows"),
      pwrVal: pw.querySelector(".bat-pwr-val"),
      pwrUnit: pw.querySelector(".bat-pwr-unit"),
      pwrLbl: pw.querySelector(".bat-pwr-lbl"),
      pwrBar: pw.querySelector(".bat-pwr-bar"),
      pwrTrack: pw.querySelector(".bat-pwr-track"),
      pwrAvail: pw.querySelector(".bat-pwr-avail"),
      cap: pw.querySelector(".bat-cap"),
      acTitle: pw.querySelector(".bat-ac-title"),
      acVal: pw.querySelector(".bat-ac-val"),
      acUnit: pw.querySelector(".bat-ac-unit"),
      acSub: pw.querySelector(".bat-ac-sub"),
      cellVal: pw.querySelector(".bat-cell-val"),
      cellUnit: pw.querySelector(".bat-cell-unit"),
      cellSub: pw.querySelector(".bat-cell-sub"),
      mpptTotal: pw.querySelector(".bat-mppt-total-val"),
      ogWrap: pw.querySelector(".bat-offgrid"),
      ogVal: pw.querySelector(".bat-og-val"),
      ogUnit: pw.querySelector(".bat-og-unit"),
      M,
      chV: ebody.querySelector(".bat-ch-v"),
      chBar: ebody.querySelector(".bat-ch-bar"),
      disV: ebody.querySelector(".bat-dis-v"),
      disBar: ebody.querySelector(".bat-dis-bar"),
      mpptSect: mppt,
      mpptChips: mppt.querySelector(".bat-mppt-chips"),
      ctlGrid: controls.querySelector(".bat-ctl-grid"),
      ctlSig: null,
      controls: {},
      infoGrid: info.querySelector(".bat-info-grid"),
    };
    return card;
  }

  _patchBatteries(list) {
    if (!this._batCards) return;
    const sig = list.map((b) => b.dev).sort().join("|");
    if (sig !== this._batSig && this._main) {
      // battery set changed under us: rebuild the whole view, then patch fresh
      this._main.innerHTML = "";
      this._main.appendChild(this._renderBaterias());
      list = this._batteryModel();
    }
    for (const b of list) {
      const r = this._batCards[b.dev];
      if (r) this._patchBatteryCard(r, b);
    }
  }

  _patchBatteryCard(r, b) {
    r.name.textContent = b.name || this._t("battery");

    // inverter-state chip (localized; tone by state)
    const inv = b.inverter;
    const invState = this._sval(inv);
    if (invState) {
      const raw = invState.toLowerCase();
      let tone = "neutral", disp;
      // inverter_state exposes the English label (sensor.py states map); localize
      // here since HA has no state translation for these free-text values.
      if (raw.includes("backup")) { disp = this._t("invBackup"); tone = "warn"; }
      else if (raw.includes("ota") || raw.includes("upgrade")) { disp = this._t("invUpdating"); tone = "warn"; }
      else if (raw.includes("discharge")) { disp = this._t("discharging"); tone = "good"; }
      else if (raw.includes("charge")) { disp = this._t("charging"); tone = "good"; }
      else if (raw.includes("standby")) disp = this._t("invStandby");
      else if (raw.includes("sleep")) disp = this._t("idle");
      else if (raw.includes("bypass")) disp = this._t("invBypass");
      else disp =
        typeof this._hass.formatEntityState === "function"
          ? this._hass.formatEntityState(inv)
          : invState;
      if (b.hasMppt) disp = this._t("inverterMode", { state: disp });
      this._setChip(r.state, disp, tone);
      r.state.style.display = "";
    } else if (b.powerW != null) {
      // No inverter_state sensor (e.g. Zendure): derive the chip from power flow.
      const w = b.powerW;
      const disp = w > 30 ? this._t("charging") : w < -30 ? this._t("discharging") : this._t("invStandby");
      this._setChip(r.state, disp, w > 30 || w < -30 ? "good" : "neutral");
      r.state.style.display = "";
    } else {
      r.state.style.display = "none";
    }

    // SOC ring
    if (b.soc != null) {
      r.ringFg.setAttribute(
        "stroke-dashoffset",
        (r.ringCirc * (1 - this._clamp(b.soc, 0, 100) / 100)).toFixed(2)
      );
      r.ringVal.innerHTML = Math.round(b.soc) + "<span>%</span>";
    } else {
      r.ringVal.innerHTML = "—<span>%</span>";
    }
    const col = this._socColor(b.soc);
    r.ringFg.setAttribute("stroke", col);
    r.ringFg.style.filter = `drop-shadow(0 0 6px ${col})`;

    // Power readout (+ charge / - discharge). Venus A/D gets a dual view:
    // AC-port output/input on the left and net cell flow on the right.
    const w = b.powerW;
    const charging = w != null && w > 30;
    const discharging = w != null && w < -30;
    const f = this._fmtPower(w == null ? null : Math.abs(w));
    r.pwrVal.textContent = f.v;
    r.pwrUnit.textContent = f.u ? " " + f.u : "";
    let lbl = this._t("idle"), pcol = "var(--ink)";
    if (charging) { lbl = this._t("charging"); pcol = "var(--battery)"; }
    else if (discharging) { lbl = this._t("discharging"); pcol = "var(--grid)"; }
    r.pwrLbl.textContent = lbl;
    r.pwrVal.style.color = pcol;
    r.powerRoot.classList.toggle("has-mppt", b.hasMppt);
    r.standardFlow.style.display = b.hasMppt ? "none" : "";
    r.mpptFlows.style.display = b.hasMppt ? "" : "none";
    r.pwrTrack.style.display = b.hasMppt ? "none" : "";
    r.pwrAvail.style.display = b.hasMppt ? "none" : "";

    if (b.hasMppt) {
      const ac = b.acFlowW;
      const acOutput = ac != null && ac > 30;
      const acInput = ac != null && ac < -30;
      const af = this._fmtPower(ac == null ? null : Math.abs(ac));
      r.acTitle.textContent = this._t(acInput ? "acInput" : "acOutput");
      r.acVal.textContent = af.v;
      r.acUnit.textContent = af.u ? " " + af.u : "";
      r.acSub.textContent = acOutput
        ? this._t("toHomeGrid")
        : acInput
          ? this._t("fromAcBus")
          : this._t("idle");
      r.acVal.style.color = acOutput ? "var(--grid)" : acInput ? "var(--battery)" : "var(--ink)";

      r.cellVal.textContent = f.v;
      r.cellUnit.textContent = f.u ? " " + f.u : "";
      r.cellSub.textContent = lbl;
      r.cellVal.style.color = pcol;
      r.mpptTotal.textContent = this._fmtPowerStr(b.mpptTotalW);
    }
    let tcap = charging ? b.maxCharge : discharging ? b.maxDischarge : b.maxCharge || b.maxDischarge;
    if (!tcap) tcap = 2500;
    r.pwrBar.style.width = this._clamp((Math.abs(w || 0) / tcap) * 100, 0, 100) + "%";
    r.pwrBar.style.background = discharging ? "var(--grid)" : "var(--battery)";
    const ftc = this._fmtPower(tcap);
    r.pwrAvail.textContent = this._t("availOf", { value: `${ftc.v} ${ftc.u}` });
    r.cap.textContent = `${this._nf(b.stored, 2)} / ${this._nf(b.capacity, 2)} kWh`;

    // off-grid power — only while the backup function switch is on
    if (b.backupOn && b.offgridW != null) {
      const fo = this._fmtPower(b.offgridW);
      r.ogVal.textContent = fo.v;
      r.ogUnit.textContent = fo.u ? " " + fo.u : "";
      r.ogWrap.style.display = "";
    } else {
      r.ogWrap.style.display = "none";
    }

    // health / cells
    const M = r.M;
    M.temp.textContent = b.temp != null ? `${this._nf(b.temp, 1)} °C` : "—";
    M.volt.textContent = b.voltage != null ? `${this._nf(b.voltage, 2)} V` : "—";
    M.cmax.textContent = b.cellMax != null ? `${this._nf(b.cellMax, 3)} V` : "—";
    M.cmin.textContent = b.cellMin != null ? `${this._nf(b.cellMin, 3)} V` : "—";
    if (b.cellDelta != null) {
      const d = b.cellDelta;
      M.cdelta.textContent = `${Math.round(d)} mV`;
      // tiers mirror const.py BALANCE_THRESHOLD_YELLOW/ORANGE/RED (raw delta)
      M.cdelta.style.color =
        d >= DELTA_MV_RED ? "oklch(0.7 0.18 25)"
          : d >= DELTA_MV_ORANGE ? "oklch(0.72 0.16 50)"
            : d >= DELTA_MV_YELLOW ? "oklch(0.82 0.14 75)"
              : "";
    } else {
      M.cdelta.textContent = "—";
      M.cdelta.style.color = "";
    }
    // cycles: prefer the BMS modbus register; fall back to the calculated sensor
    // when the model exposes no cycle-count register.
    const cyc = b.cycles != null ? b.cycles : b.cyclesCalc;
    M.cycles.textContent = cyc != null ? Math.round(cyc) : "—";
    M.rte.textContent = b.rte != null ? `${this._nf(b.rte, 1)} %` : "—";
    // charge hysteresis active state ("—" when the sensor isn't exposed)
    if (b.hysteresisActive == null) {
      M.hyst.textContent = "—";
      M.hyst.style.color = "";
    } else {
      M.hyst.textContent = b.hysteresisActive ? this._t("active") : this._t("inactive");
      M.hyst.style.color = b.hysteresisActive ? "oklch(0.82 0.14 75)" : "";
    }

    // energía hoy
    const u = `<span class="dim" style="font-size:11px"> kWh</span>`;
    const max = Math.max(b.dailyCharge || 0, b.dailyDischarge || 0, 0.1);
    r.chV.innerHTML = `${this._nf(b.dailyCharge, 2)}${u}`;
    r.chBar.style.width = ((b.dailyCharge || 0) / max) * 100 + "%";
    r.disV.innerHTML = `${this._nf(b.dailyDischarge, 2)}${u}`;
    r.disBar.style.width = ((b.dailyDischarge || 0) / max) * 100 + "%";

    // solar (MPPT)
    if (b.hasMppt) {
      r.mpptSect.style.display = "";
      r.mpptChips.innerHTML = b.mppt
        .map((v, i) =>
          v == null ? null : `<span class="chip mppt-chip">MPPT${i + 1} · ${this._fmtPowerStr(v)}</span>`
        )
        .filter(Boolean)
        .join("");
    } else {
      r.mpptSect.style.display = "none";
    }

    // info (firmware / wifi / mac)
    const rows = [];
    const addRow = (label, val) => {
      if (val != null && val !== "")
        rows.push(`<div class="info-row"><span class="muted">${label}</span><span>${val}</span></div>`);
    };
    addRow(this._t("infoManufacturer"), b.info.manufacturer);
    addRow(this._t("infoModel"), b.model);
    addRow(this._t("infoSunSpecModel"), b.info.sunspecModelType);
    addRow(this._t("infoSoftware"), b.info.sw);
    addRow("BMS", b.info.bms);
    addRow("VMS", b.info.vms);
    addRow("EMS", b.info.ems);
    addRow(this._t("infoComm"), b.info.comm);
    let wifi = b.info.wifiSignal != null ? `${Math.round(b.info.wifiSignal)} dBm` : null;
    const wstat = this._sval(b.info.wifiStatus);
    if (wstat) {
      const wdisp =
        typeof this._hass.formatEntityState === "function"
          ? this._hass.formatEntityState(b.info.wifiStatus)
          : wstat;
      wifi = wifi ? `${wifi} · ${wdisp}` : wdisp;
    }
    addRow("WiFi", wifi);
    addRow("MAC", b.info.mac);
    // A Huawei storage is three kinds of hardware — inverter, power module,
    // packs — and each carries its own serial and firmware. Brands with a
    // single identity keep the plain serial row.
    if (b.info.powerModuleFw || b.info.inverterSn) {
      addRow(
        this._t("infoInverter"),
        [b.info.inverterModel, b.info.inverterSn, b.info.inverterFw].filter(Boolean).join(" · ")
      );
      addRow(this._t("infoPowerModule"), [b.info.serial, b.info.powerModuleFw].filter(Boolean).join(" · "));
    } else {
      addRow(this._t("infoSerial"), b.info.serial);
    }
    for (const pack of b.info.packs || [])
      addRow(`Pack ${pack.n}`, [pack.sn, pack.fw].filter(Boolean).join(" · "));
    r.infoGrid.innerHTML = rows.length ? rows.join("") : `<div class="dim">${this._t("noData")}</div>`;

    // controls (rebuilt when the available-control set changes; else value-patched)
    this._syncControls(r, b);
  }

  // ----- per-battery controls -----------------------------------------------
  /** Localized label for a select option (uses HA's state override formatter). */
  _fmtOption(stateObj, option) {
    if (typeof this._hass.formatEntityState === "function") {
      try { return this._hass.formatEntityState(stateObj, option); } catch (e) { /* fall through */ }
    }
    return option;
  }

  _syncControls(r, b) {
    const hass = this._hass;
    const controlId = (c) =>
      (b.entIdsDomain && b.entIdsDomain[`${c.domain}:${c.key}`]) ||
      (b.entIds[c.key] && b.entIds[c.key].startsWith(c.domain + ".")
        ? b.entIds[c.key]
        : null);
    const avail = BAT_CONTROLS.filter((c) => {
      const id = controlId(c);
      const st = id && hass.states[id];
      // Hide controls with no live value (e.g. stale registry entities left from
      // re-adding a device under a different driver) — their slider is dead anyway.
      return st && st.state !== "unavailable" && st.state !== "unknown";
    });
    const sig = avail.map((c) => c.key).join("|");
    if (sig !== r.ctlSig) {
      r.ctlSig = sig;
      r.controls = {};
      r.ctlGrid.innerHTML = "";
      if (!avail.length) {
        const e = document.createElement("div");
        e.className = "dim ctl-empty";
        e.textContent = this._t("ctlEmpty");
        r.ctlGrid.appendChild(e);
      } else {
        for (const c of avail) r.ctlGrid.appendChild(this._buildControlRow(r, b, c));
      }
    }
    for (const c of avail) {
      const w = r.controls[c.key];
      if (w) this._patchControlRow(w, hass.states[controlId(c)]);
    }
  }

  /** Returns a fragment with the control's grid items (label + control, or a
   *  full-width button), so the parent .bat-ctl-grid aligns labels/controls
   *  across rows and every slider gets the same width. */
  _buildControlRow(r, b, c) {
    const id =
      (b.entIdsDomain && b.entIdsDomain[`${c.domain}:${c.key}`]) ||
      (b.entIds[c.key] && b.entIds[c.key].startsWith(c.domain + ".")
        ? b.entIds[c.key]
        : null);
    const state = id && this._hass.states[id];
    const frag = document.createDocumentFragment();

    const cLabel = this._t(c.lk);
    if (c.domain === "button") {
      const btn = document.createElement("button");
      btn.className = "ctl-btn";
      btn.innerHTML = `<ha-icon icon="${c.icon}"></ha-icon>${cLabel}`;
      btn.addEventListener("click", () => {
        if (c.confirm && !window.confirm(`${cLabel}?`)) return;
        this._hass.callService("button", "press", { entity_id: id });
      });
      frag.appendChild(btn);
      r.controls[c.key] = { type: "button" };
      return frag;
    }

    const label = document.createElement("span");
    label.className = "ctl-k";
    label.innerHTML = `<ha-icon icon="${c.icon}"></ha-icon><span>${cLabel}</span>`;
    frag.appendChild(label);

    if (c.domain === "switch") {
      const btn = document.createElement("button");
      btn.className = "ctl-toggle";
      btn.innerHTML = `<span class="ctl-knob"></span>`;
      btn.addEventListener("click", () =>
        this._hass.callService("switch", "toggle", { entity_id: id })
      );
      frag.appendChild(btn);
      r.controls[c.key] = { type: "switch", el: btn };
    } else if (c.domain === "select") {
      const sel = document.createElement("select");
      sel.className = "ctl-select";
      sel.addEventListener("change", () =>
        this._hass.callService("select", "select_option", { entity_id: id, option: sel.value })
      );
      frag.appendChild(sel);
      r.controls[c.key] = { type: "select", el: sel };
    } else {
      // number → slider + value
      const wrap = document.createElement("div");
      wrap.className = "ctl-num";
      const range = document.createElement("input");
      range.type = "range";
      this._wireRangeInteraction(range);
      const valEl = document.createElement("span");
      valEl.className = "ctl-val";
      const a = (state && state.attributes) || {};
      const unit = a.unit_of_measurement || "";
      range.addEventListener("input", () => {
        range.__sliding = true;
        valEl.textContent = `${Math.round(this._clampToEntity(id, range.value))}${unit ? " " + unit : ""}`;
      });
      range.addEventListener("change", () => {
        const value = this._clampToEntity(id, range.value);
        this._markRangePending(range, value);
        this._hass.callService("number", "set_value", {
          entity_id: id,
          value,
        });
      });
      wrap.appendChild(range);
      wrap.appendChild(valEl);
      frag.appendChild(wrap);
      r.controls[c.key] = { type: "number", el: range, val: valEl };
    }
    return frag;
  }

  _patchControlRow(w, state) {
    if (!state || w.type === "button") return;
    const focused = this.shadowRoot && this.shadowRoot.activeElement === w.el;
    if (w.type === "switch") {
      w.el.classList.toggle("on", state.state === "on");
    } else if (w.type === "select") {
      const opts = Array.isArray(state.attributes.options) ? state.attributes.options : [];
      const sig = opts.join("|");
      if (w.el.__opts !== sig) {
        w.el.__opts = sig;
        w.el.innerHTML = opts
          .map((o) => `<option value="${o}">${this._fmtOption(state, o)}</option>`)
          .join("");
      }
      if (!focused) w.el.value = state.state;
    } else if (w.type === "number") {
      const a = state.attributes || {};
      if (a.step != null) w.el.step = a.step;
      // Floor min to a step boundary so the grid is absolute multiples of step
      // (matches HA's number slider, e.g. 12,15,20,…); commits clamp to real min.
      if (a.min != null) w.el.min = this._sliderMin(a.min, a.step != null ? a.step : 1);
      if (a.max != null) w.el.max = a.max;
      const unit = a.unit_of_measurement || "";
      if (!this._rangePatchLocked(w.el, state, a.step)) {
        const v = Number(state.state);
        if (!Number.isNaN(v)) w.el.value = v;
        // Show the real state value, not w.el.value: a native range input snaps
        // its value to the min+k*step grid, so an off-grid state (e.g. 60 with
        // min 12 / step 5) would otherwise display as the snapped 62.
        w.val.textContent =
          state.state == null || state.state === "unknown" || state.state === "unavailable"
            ? "—"
            : `${Math.round(v)}${unit ? " " + unit : ""}`;
      }
    }
  }

  // ===== Control view ========================================================
  // A sectioned list of system-level entities grouped by feature (switch + its
  // related CONFIG params), matched by translation_key and resolved by entity_id,
  // reusing the per-battery control widgets/CSS.

  _renderControl() {
    this._ctlStore = {};
    const { wrap, sig } = this._renderSysSections(SYS_SECTIONS, this._ctlStore, {
      icon: "mdi:tune-variant",
      title: this._t("sysEmptyTitle"),
      msg: this._t("sysEmptyMsg"),
    });
    this._ctlSig = sig;
    // Only offer drag-to-arrange when there are real cards (not the empty state).
    if (!wrap.querySelector(".card")) return wrap;
    // Hidden cards move to their own stack BEFORE layout so the grid/matrix only
    // places the visible ones. Each card gets its eye toggle (arrange mode only).
    const hiddenSet = new Set(this._loadCtlHidden());
    const hiddenStack = document.createElement("div");
    hiddenStack.className = "sys-stack ctl-hidden-stack";
    for (const card of [...wrap.querySelectorAll(".card")]) {
      const isHidden = hiddenSet.has(card.dataset.tk);
      this._addHideBtn(card, isHidden);
      if (isHidden) hiddenStack.appendChild(card);
    }
    // Layout: a fixed column+row count switches to a manual C×R matrix (drag any
    // card into any cell, empty cells included); otherwise a responsive flow grid
    // (fixed-or-auto columns, drag reorders the sequence).
    if (this._isMatrixMode()) this._layoutMatrix(wrap);
    else this._applyCtlGrid(wrap);
    const root = document.createElement("div");
    root.className = "ctl-root";
    this._ctlRoot = root; // _applyArrangeMode toggles .arranging here too
    root.appendChild(this._buildArrangeBar(wrap));
    root.appendChild(wrap);
    if (hiddenStack.childElementCount) {
      const sec = document.createElement("div");
      sec.className = "ctl-hidden";
      sec.innerHTML =
        `<div class="ctl-hidden-title"><ha-icon icon="mdi:eye-off-outline"></ha-icon>` +
        `<span>${this._t("ctlHidden")}</span></div>`;
      sec.appendChild(hiddenStack);
      root.appendChild(sec);
    }
    return root;
  }

  /** Eye toggle in the card header: hides the card into the "Hidden cards"
   *  section (or restores it). Only visible while arrange mode is ON (CSS). */
  _addHideBtn(card, isHidden) {
    const btn = document.createElement("button");
    btn.className = "ctl-hide-btn";
    btn.title = this._t(isHidden ? "ctlShow" : "ctlHide");
    btn.innerHTML = `<ha-icon icon="mdi:${isHidden ? "eye" : "eye-off"}-outline"></ha-icon>`;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const set = new Set(this._loadCtlHidden());
      if (isHidden) set.delete(card.dataset.tk);
      else set.add(card.dataset.tk);
      this._saveCtlHidden([...set]);
      this._rebuildControl();
    });
    card.querySelector(".card-head").appendChild(btn);
  }

  /** Manual matrix is active only when BOTH a column and a row count are pinned;
   *  a single axis (or Auto) stays on the responsive flow grid. */
  _isMatrixMode() { return this._loadCtlCols() >= 1 && this._loadCtlRows() >= 1; }

  /** Rebuild the whole Control view in place (used when a stepper changes, since
   *  switching flow↔matrix or track counts restructures the DOM, not just CSS). */
  _rebuildControl() {
    if (this._view !== "control" || !this._main) return;
    this._main.innerHTML = "";
    this._main.appendChild(this._renderControl());
  }

  /** Toolbar above the Control grid: the arrange-mode toggle. While ON, cards
   *  are draggable and their inner controls are locked (so a drag never grabs a
   *  slider); OFF restores normal interaction. State is sticky across rebuilds. */
  _buildArrangeBar(stack) {
    const bar = document.createElement("div");
    bar.className = "ctl-bar";
    const hint = document.createElement("span");
    hint.className = "ctl-hint";
    bar.appendChild(hint);
    const tools = document.createElement("div");
    tools.className = "ctl-tools";
    tools.append(
      this._buildStepper(this._t("ctlCols"), () => this._loadCtlCols(), (n) => this._saveCtlCols(n), 5, 3),
      this._buildStepper(this._t("ctlRows"), () => this._loadCtlRows(), (n) => this._saveCtlRows(n), 8, 4),
    );
    bar.appendChild(tools);
    const btn = document.createElement("button");
    btn.className = "ctl-arrange-btn";
    btn.innerHTML = `<ha-icon icon="mdi:drag-variant"></ha-icon><span>${this._t("ctlArrange")}</span>`;
    btn.addEventListener("click", () => {
      this._arrangeMode = !this._arrangeMode;
      this._applyArrangeMode(stack, btn, hint, tools);
    });
    bar.appendChild(btn);
    this._applyArrangeMode(stack, btn, hint, tools); // restore sticky state on rebuild
    return bar;
  }

  /** A small `[label − N +]` stepper. `load`/`save` read & persist the value
   *  (0 = Auto). From Auto, the first increment jumps to `autoStart`; decrement
   *  past 1 returns to Auto; `max` clamps the top. Each change rebuilds the
   *  Control view. Used for both the column- and row-count controls. */
  _buildStepper(label, load, save, max, autoStart) {
    const box = document.createElement("div");
    box.className = "ctl-cols";
    const lbl = document.createElement("span");
    lbl.className = "ctl-cols-lbl";
    lbl.textContent = label;
    const dec = document.createElement("button");
    dec.type = "button";
    dec.innerHTML = `<ha-icon icon="mdi:minus"></ha-icon>`;
    const val = document.createElement("span");
    val.className = "ctl-cols-val";
    const inc = document.createElement("button");
    inc.type = "button";
    inc.innerHTML = `<ha-icon icon="mdi:plus"></ha-icon>`;
    const refresh = () => {
      const n = load();
      val.textContent = n >= 1 ? String(n) : this._t("ctlAuto");
    };
    dec.addEventListener("click", () => {
      const n = load();
      save(n >= 1 ? n - 1 : 0); // 1 → 0 returns to Auto
      refresh();
      this._rebuildControl();
    });
    inc.addEventListener("click", () => {
      const n = load();
      save(Math.min((n || (autoStart - 1)) + 1, max)); // from Auto → autoStart
      refresh();
      this._rebuildControl();
    });
    box.append(lbl, dec, val, inc);
    refresh();
    return box;
  }

  _applyArrangeMode(stack, btn, hint, tools) {
    const on = !!this._arrangeMode;
    stack.classList.toggle("arranging", on);
    if (this._ctlRoot) this._ctlRoot.classList.toggle("arranging", on);
    btn.classList.toggle("active", on);
    hint.textContent = on ? this._t("ctlArrangeHint") : "";
    if (tools) tools.style.display = on ? "" : "none";
    for (const card of stack.querySelectorAll(".card")) card.draggable = on;
  }
  _patchControl() {
    this._patchSysView(SYS_SECTIONS, "_ctlStore", "_ctlSig", "control", () => this._renderControl());
  }

  /** Scan section defs against the live registry: which entities exist + a
   *  signature of the available set (so the view rebuilds when it changes). */
  _sysScan(defs) {
    const { byKey } = this._index();
    const sections = [];
    const sigParts = [];
    for (const sec of defs) {
      const rows = [];
      for (const item of sec.items) {
        const ids = byKey.get(item.key) || [];
        for (const id of ids) {
          // Skip registry leftovers of a de-configured feature (e.g. pricing
          // entities after switching back to time slots): HA restores them
          // with an unavailable state, which would render as dead rows.
          // Buttons sit at "unknown" until first pressed — that's normal, not a
          // leftover — so for them only "unavailable" means truly gone.
          const st = this._hass.states[id];
          const alive = item.domain === "button"
            ? st && st.state !== "unavailable"
            : st && st.state !== "unavailable" && st.state !== "unknown";
          if (alive) {
            rows.push({ item, id, multi: ids.length > 1 });
          }
        }
      }
      // Excluded-device controls arrive grouped by their translation key (all
      // "Enabled" switches, then all "Solar surplus" switches, etc.). Sort
      // their live entities by the displayed name instead, so adding a device
      // does not leave the card in registry/control-type order.
      if (sec.tk === "secExcluded") {
        rows.sort((a, b) => {
          const aName = this._entityShortName(this._hass.states[a.id], a.id);
          const bName = this._entityShortName(this._hass.states[b.id], b.id);
          return aName.localeCompare(bName, this._lang()) || a.id.localeCompare(b.id);
        });
      }
      if (rows.length) {
        sections.push({ sec, rows });
        sigParts.push(sec.tk + ":" + rows.map((r) => r.id).join(","));
      }
    }
    return { sections, sig: sigParts.join("|") };
  }

  /** Build the sectioned card stack into `store` (id -> widget). */
  _renderSysSections(defs, store, empty) {
    for (const k in store) delete store[k];
    const { sections, sig } = this._sysScan(defs);
    const wrap = document.createElement("div");
    wrap.className = "sys-stack";
    if (!sections.length) {
      const e = document.createElement("div");
      e.className = "placeholder";
      e.innerHTML =
        `<ha-icon icon="${empty.icon}"></ha-icon><h3>${empty.title}</h3><p>${empty.msg}</p>`;
      wrap.appendChild(e);
      return { wrap, sig };
    }
    // Build one card per live section, keyed by tk. Each card is an independent
    // box in the responsive grid (flattened layout) so it can be drag-reordered.
    const cardByTk = {};
    for (const { sec, rows } of sections) {
      const { card, head } = this._card(this._t(sec.tk), sec.icon || "mdi:cog-outline");
      card.dataset.tk = sec.tk;
      this._attachHelp(head, this._help(sec.tk));
      const grid = document.createElement("div");
      grid.className = "bat-ctl-grid sys-grid";
      // A `gate` switch (e.g. predictive_charging) hides its sibling param rows
      // when OFF: the feature's sliders disappear, the switch stays so it can be
      // turned back on. `gateInvert` flips this (PD section: show when no_pd_mode
      // is OFF). _patchSysControl keeps this in sync on state changes.
      let gateKey = null;
      const gatedNodes = [];
      for (const r of rows) {
        const frag = this._buildSysControl(r.item, r.id, store, r.multi);
        const nodes = [...frag.childNodes];
        grid.appendChild(frag);
        if (r.item.gate) gateKey = this._sysStoreKey(r.item, r.id);
        else gatedNodes.push(...nodes);
      }
      if (gateKey && gatedNodes.length && store[gateKey]) {
        const w = store[gateKey];
        w.gatedNodes = gatedNodes;
        const on = (this._hass.states[w.realId || gateKey] || {}).state === "on";
        const shown = w.invert ? !on : on;
        for (const n of gatedNodes) n.style.display = shown ? "" : "none";
      }
      if (sec.tk === "secHourly") {
        const warn = this._hourlyWarnEl();
        if (warn) card.appendChild(warn);
      }
      card.appendChild(grid);
      this._makeCardDraggable(card, wrap);
      cardByTk[sec.tk] = card;
    }
    // Place cards in the user's saved order (drag-and-drop persists it), seeded
    // by the default layout order, with any new/unknown sections appended.
    const seen = new Set();
    const order = [];
    const push = (tk) => { if (cardByTk[tk] && !seen.has(tk)) { order.push(tk); seen.add(tk); } };
    for (const tk of (this._loadCtlOrder() || [])) push(tk);
    for (const tk of DEFAULT_SYS_ORDER) push(tk);
    for (const { sec } of sections) push(sec.tk);
    for (const tk of order) wrap.appendChild(cardByTk[tk]);
    return { wrap, sig };
  }

  /** Inline banner for the Hourly Balance card: the feature only applies under
   *  Spain's hourly surplus-compensation scheme (RD 244/2019). Shown only when HA
   *  is configured for a confirmed non-ES country, to deter accidental use abroad.
   *  Returns null (no banner) when the country is ES or unset. */
  _hourlyWarnEl() {
    const c = (this._hass && this._hass.config && this._hass.config.country) || "";
    if (!c || c.toUpperCase() === "ES") return null;
    const el = document.createElement("div");
    el.className = "sys-warn";
    el.textContent = "⚠️ " + this._t("hourlyEsOnly", { c });
    el.style.cssText =
      "margin:2px 0 8px;padding:6px 9px;border-radius:8px;font-size:12px;line-height:1.35;" +
      "background:rgba(255,170,0,.12);color:var(--warning-color,#e8a300);" +
      "border:1px solid rgba(255,170,0,.35);";
    return el;
  }

  // --- Control-tab column count (fixed-width override, persisted per browser) --
  _ctlColsKey() { return "omnibattery:control-columns"; }
  /** Saved column count, or 0 = Auto (responsive auto-fit default). */
  _loadCtlCols() {
    const n = parseInt(localStorage.getItem(this._ctlColsKey()), 10);
    return Number.isFinite(n) && n >= 1 && n <= 5 ? n : 0;
  }
  _saveCtlCols(n) {
    try {
      if (n >= 1) localStorage.setItem(this._ctlColsKey(), String(n));
      else localStorage.removeItem(this._ctlColsKey());
    } catch { /* private mode */ }
  }
  _ctlRowsKey() { return "omnibattery:control-rows"; }
  /** Saved cards-per-column count, or 0 = Auto (row-major flow, no row cap). */
  _loadCtlRows() {
    const n = parseInt(localStorage.getItem(this._ctlRowsKey()), 10);
    return Number.isFinite(n) && n >= 1 && n <= 8 ? n : 0;
  }
  _saveCtlRows(n) {
    try {
      if (n >= 1) localStorage.setItem(this._ctlRowsKey(), String(n));
      else localStorage.removeItem(this._ctlRowsKey());
    } catch { /* private mode */ }
  }
  /** Flow grid: pin a fixed column count (minmax(340px, 1fr) keeps cards usable
   *  yet stretching) or, when Auto, fall back to the CSS auto-fit default. Cards
   *  stay a single drag-reorderable sequence. (Row count only matters in matrix
   *  mode — see _layoutMatrix.) */
  _applyCtlGrid(stack) {
    const c = this._loadCtlCols();
    stack.style.gridTemplateColumns = c >= 1 ? `repeat(${c}, minmax(340px, 1fr))` : "";
  }

  // --- Control-tab manual matrix (drag cards into explicit C×R cells) ----------
  _ctlCellsKey() { return "omnibattery:control-cells"; }
  /** Saved card→cell map: { [tk]: { c, r } }. */
  _loadCells() {
    try {
      const v = JSON.parse(localStorage.getItem(this._ctlCellsKey()));
      return v && typeof v === "object" ? v : {};
    } catch { return {}; }
  }
  /** Persist the current cell occupancy by reading each cell's card back. */
  _saveCells(stack) {
    const map = {};
    for (const cell of stack.querySelectorAll(".ctl-cell")) {
      const card = cell.firstElementChild;
      if (card && card.dataset.tk) map[card.dataset.tk] = { c: +cell.dataset.c, r: +cell.dataset.r };
    }
    try { localStorage.setItem(this._ctlCellsKey(), JSON.stringify(map)); } catch { /* private mode */ }
  }

  /** Lay the cards out as a manual C×R matrix: build C·R empty cell drop-zones,
   *  place each card in its saved cell, then fill any unplaced/overflow cards into
   *  the first free cells. Rows grow past the requested count if needed so no card
   *  is ever lost. Empty cells stay as valid drop targets while arranging. */
  _layoutMatrix(stack) {
    const C = this._loadCtlCols();
    const cards = [...stack.querySelectorAll(".card")];
    const R = Math.max(this._loadCtlRows(), Math.ceil(cards.length / C));
    const saved = this._loadCells();
    for (const card of cards) card.remove();
    stack.classList.add("matrix");
    stack.style.gridTemplateColumns = `repeat(${C}, minmax(340px, 1fr))`;
    stack.style.gridTemplateRows = `repeat(${R}, min-content)`;
    const cells = [];
    for (let r = 0; r < R; r++) {
      for (let c = 0; c < C; c++) {
        const cell = document.createElement("div");
        cell.className = "ctl-cell";
        cell.dataset.c = c;
        cell.dataset.r = r;
        this._wireCell(cell, stack);
        stack.appendChild(cell);
        cells.push(cell);
      }
    }
    // Place cards honoring saved positions first; collect the rest as leftovers.
    const leftover = [];
    for (const card of cards) {
      const pos = saved[card.dataset.tk];
      const cell = pos && pos.c < C && pos.r < R ? cells[pos.r * C + pos.c] : null;
      if (cell && !cell.firstElementChild) cell.appendChild(card);
      else leftover.push(card);
    }
    let idx = 0;
    for (const card of leftover) {
      while (idx < cells.length && cells[idx].firstElementChild) idx++;
      if (idx < cells.length) cells[idx].appendChild(card);
    }
  }

  /** Wire a matrix cell as a drop target: dropping a dragged card moves it here,
   *  swapping with any current occupant back into the card's old cell. */
  _wireCell(cell, stack) {
    cell.addEventListener("dragover", (e) => {
      if (!this._arrangeMode || !this._dragEl) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      cell.classList.add("drop-target");
    });
    cell.addEventListener("dragleave", () => cell.classList.remove("drop-target"));
    cell.addEventListener("drop", (e) => {
      if (!this._arrangeMode || !this._dragEl) return;
      e.preventDefault();
      cell.classList.remove("drop-target");
      const dragged = this._dragEl;
      const occupant = cell.firstElementChild;
      if (occupant === dragged) return;
      const from = dragged.parentElement;
      if (occupant) from.appendChild(occupant); // swap into the vacated cell
      cell.appendChild(dragged);
      this._saveCells(stack);
    });
  }

  // --- Control-tab card reordering (drag-and-drop, persisted per browser) -----
  _ctlOrderKey() { return "omnibattery:control-order"; }
  _loadCtlOrder() {
    try {
      const v = JSON.parse(localStorage.getItem(this._ctlOrderKey()));
      return Array.isArray(v) ? v : null;
    } catch { return null; }
  }
  _saveCtlOrder(stack) {
    const order = [...stack.querySelectorAll(".card")].map((c) => c.dataset.tk).filter(Boolean);
    try { localStorage.setItem(this._ctlOrderKey(), JSON.stringify(order)); } catch { /* private mode */ }
  }
  // --- Control-tab hidden cards (eye toggle in arrange mode, persisted) -------
  _ctlHiddenKey() { return "omnibattery:control-hidden"; }
  _loadCtlHidden() {
    try {
      const v = JSON.parse(localStorage.getItem(this._ctlHiddenKey()));
      return Array.isArray(v) ? v : [];
    } catch { return []; }
  }
  _saveCtlHidden(tks) {
    try { localStorage.setItem(this._ctlHiddenKey(), JSON.stringify(tks)); } catch { /* private mode */ }
  }
  /** Wire HTML5 drag events on a card. Active only while arrange mode is ON
   *  (card.draggable is toggled by _applyArrangeMode). In flow mode it reorders
   *  the DOM sequence live; in matrix mode the cell drop-zones own placement so
   *  the sequence handlers stand down (dragstart/dragend stay shared). */
  _makeCardDraggable(card, stack) {
    card.addEventListener("dragstart", (e) => {
      if (!this._arrangeMode) { e.preventDefault(); return; }
      this._dragEl = card;
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", card.dataset.tk || ""); } catch { /* IE */ }
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      this._dragEl = null;
      if (!this._isMatrixMode()) this._saveCtlOrder(stack);
    });
    card.addEventListener("dragover", (e) => {
      if (this._isMatrixMode()) return; // cells handle placement
      if (card.parentElement !== stack) return; // card lives in the hidden stack
      if (!this._arrangeMode || !this._dragEl || this._dragEl === card) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      const r = card.getBoundingClientRect();
      const before = e.clientX < r.left + r.width / 2;
      stack.insertBefore(this._dragEl, before ? card : card.nextSibling);
    });
    card.addEventListener("drop", (e) => e.preventDefault());
  }

  /** Patch all widgets in a system view; rebuild it if the available set changed. */
  _patchSysView(defs, storeKey, sigKey, view, renderFn) {
    const store = this[storeKey];
    if (!store) return;
    const sig = this._sysScan(defs).sig;
    if (sig !== this[sigKey] && this._main && this._view === view) {
      this._main.innerHTML = "";
      this._main.appendChild(renderFn()); // resets store + sig
    }
    for (const [id, w] of Object.entries(this[storeKey])) {
      const st = this._hass.states[w.realId || id];
      if (st) this._patchSysControl(w, st);
    }
  }

  /** Slider grid floor: a native <input type=range> snaps to min+k*step, but HA
   *  number sliders snap to absolute multiples of step (e.g. min 12 / step 5 →
   *  12,15,20,…,90). Flooring the element's min to a step boundary reproduces
   *  that grid; commits/display are clamped back to the real min. */
  _sliderMin(min, step) {
    const m = Number(min), s = Number(step);
    if (Number.isNaN(m) || Number.isNaN(s) || s <= 0) return min;
    return Math.floor(m / s) * s;
  }
  /** Clamp a slider value to the entity's real [min,max] (live attributes). */
  _clampToEntity(id, value) {
    const a = ((this._hass.states[id] || {}).attributes) || {};
    let v = Number(value);
    if (a.min != null) v = Math.max(Number(a.min), v);
    if (a.max != null) v = Math.min(Number(a.max), v);
    return v;
  }

  /** Track range interaction explicitly: iOS Safari does not reliably focus a
   *  range input on touch, so activeElement alone cannot protect a drag from
   *  the frequent HA state patches. */
  _wireRangeInteraction(range) {
    const begin = () => { range.__sliding = true; };
    const end = () => { range.__sliding = false; };
    range.addEventListener("pointerdown", begin);
    range.addEventListener("pointerup", end);
    range.addEventListener("pointercancel", end);
    // Older iOS versions may expose touch events without Pointer Events.
    range.addEventListener("touchstart", begin, { passive: true });
    range.addEventListener("touchend", end, { passive: true });
    range.addEventListener("touchcancel", end, { passive: true });
  }

  /** Keep the thumb at the submitted value until HA echoes it back. Without
   *  this short optimistic window, an update carrying the previous entity
   *  state can snap the slider back immediately after the finger is lifted. */
  _markRangePending(range, value) {
    range.__sliding = false;
    range.__pendingValue = Number(value);
    range.__pendingUntil = Date.now() + 4000;
  }

  _rangePatchLocked(range, state, step = 1) {
    const pending = Number(range.__pendingValue);
    const actual = Number(state.state);
    const tolerance = Math.max(1e-9, Math.abs(Number(step) || 1) * 1e-6);
    if (Number.isFinite(pending) && Number.isFinite(actual) && Math.abs(actual - pending) <= tolerance) {
      range.__pendingValue = undefined;
      range.__pendingUntil = 0;
    }
    if (range.__sliding) return true;
    if ((range.__pendingUntil || 0) > Date.now()) return true;
    range.__pendingValue = undefined;
    range.__pendingUntil = 0;
    return false;
  }

  /** Decimals implied by a number's step ("0.05" -> 2), capped at 3. */
  _stepDecimals(step) {
    const s = String(step);
    const i = s.indexOf(".");
    return i < 0 ? 0 : Math.min(3, s.length - i - 1);
  }
  _fmtCtlNum(value, step, unit) {
    const v = Number(value);
    const txt = Number.isNaN(v) ? "—" : v.toFixed(this._stepDecimals(step));
    return unit ? `${txt} ${unit}` : txt;
  }
  /** Friendly name minus the device prefix, for multi-instance controls. */
  _entityShortName(state, id) {
    let fn = (state && state.attributes && state.attributes.friendly_name) || id;
    const e = this._hass.entities[id];
    const dev = e && e.device_id && this._hass.devices && this._hass.devices[e.device_id];
    const dn = dev && (dev.name_by_user || dev.name);
    if (dn && fn.startsWith(dn + " ")) fn = fn.slice(dn.length + 1);
    return fn;
  }

  /** Store key for a sys control. Inverted gates get a suffixed key so the same
   *  entity can back two widgets (e.g. PD "Use PD controller" + No-PD switch). */
  _sysStoreKey(item, id) {
    return item.gateInvert ? id + "::inv" : id;
  }

  /** Build one system control's grid items (label + widget), keyed by entity_id
   *  in `store`. Mirrors _buildControlRow but resolves by id and formats numbers
   *  with step-derived decimals (PD params use fractional steps). */
  _buildSysControl(item, id, store, multi) {
    const hass = this._hass;
    const state = hass.states[id];
    const domain = item.domain || "number";
    // An inverted gate reuses the same entity as a normal gate elsewhere (e.g.
    // no_pd_mode in both PD and No-PD sections), so key its widget separately to
    // avoid one overwriting the other in the store.
    const sk = this._sysStoreKey(item, id);
    const shortName = this._entityShortName(state, id);
    const t = this._t.bind(this);
    let label = this._t(item.lk);
    if (item.labelFn) label = item.labelFn(state, t) || shortName;
    else if (multi || item.useName) label = shortName;
    const frag = document.createDocumentFragment();

    if (domain === "button") {
      const btn = document.createElement("button");
      btn.className = "ctl-btn";
      btn.innerHTML = `<ha-icon icon="${item.icon}"></ha-icon>${label}`;
      btn.addEventListener("click", () => {
        if (item.confirm && !window.confirm(`${label}?`)) return;
        hass.callService("button", "press", { entity_id: id });
      });
      frag.appendChild(btn);
      store[sk] = { type: "button" };
      return frag;
    }

    const k = document.createElement("span");
    k.className = "ctl-k";
    k.innerHTML = `<ha-icon icon="${item.icon || "mdi:cog-outline"}"></ha-icon><span>${label}</span>`;
    if (item.titleFn) {
      k.classList.add("ctl-k-info");
      // tap/click shows the detail popover — mobile has no hover. The native
      // `title` set in _patchSysControl still covers desktop hover.
      k.addEventListener("click", (e) => {
        e.stopPropagation();
        const st = (this._hass && this._hass.states && this._hass.states[id]) || state;
        this._showInfoPopover(k, item.titleFn(st, this._t.bind(this)));
      });
    } else {
      // static options-flow help (desktop hover title + tap popover for touch)
      const help = this._help(item.key);
      if (help) {
        k.classList.add("ctl-k-info");
        k.title = help;
        k.addEventListener("click", (e) => {
          e.stopPropagation();
          this._showInfoPopover(k, help);
        });
      }
    }
    frag.appendChild(k);

    if (domain === "switch") {
      const btn = document.createElement("button");
      btn.className = "ctl-toggle";
      btn.innerHTML = `<span class="ctl-knob"></span>`;
      btn.addEventListener("click", () => hass.callService("switch", "toggle", { entity_id: id }));
      frag.appendChild(btn);
      store[sk] = { type: "switch", el: btn };
      if (item.gateInvert) { store[sk].realId = id; store[sk].invert = true; }
    } else if (domain === "select") {
      const sel = document.createElement("select");
      sel.className = "ctl-select";
      sel.addEventListener("change", () =>
        hass.callService("select", "select_option", { entity_id: id, option: sel.value })
      );
      frag.appendChild(sel);
      store[sk] = { type: "select", el: sel };
    } else if (domain === "sensor" || domain === "binary_sensor") {
      // read-only verdict (e.g. PD control quality) — localized state, no input.
      // Clicking the value opens HA more-info (state history graph).
      const valEl = document.createElement("span");
      valEl.className = "ctl-val ctl-sensor";
      this._linkMoreInfo(valEl, id);
      frag.appendChild(valEl);
      store[sk] = { type: "sensor", val: valEl };
    } else {
      const wrap = document.createElement("div");
      wrap.className = "ctl-num";
      const range = document.createElement("input");
      range.type = "range";
      this._wireRangeInteraction(range);
      const valEl = document.createElement("span");
      valEl.className = "ctl-val";
      const a = (state && state.attributes) || {};
      const unit = a.unit_of_measurement || "";
      const step = Number(a.step) || 1;
      range.addEventListener("input", () => {
        range.__sliding = true;
        valEl.textContent = this._fmtCtlNum(this._clampToEntity(id, range.value), step, unit);
      });
      range.addEventListener("change", () => {
        const value = this._clampToEntity(id, range.value);
        this._markRangePending(range, value);
        hass.callService("number", "set_value", { entity_id: id, value });
      });
      wrap.appendChild(range);
      wrap.appendChild(valEl);
      frag.appendChild(wrap);
      store[sk] = { type: "number", el: range, val: valEl, step, unit };
    }
    // optional hover tooltip (set on the label cell, kept fresh on patch)
    if (item.titleFn && store[sk]) {
      store[sk].titleEl = k;
      store[sk].titleFn = item.titleFn;
    }
    return frag;
  }

  _patchSysControl(w, state) {
    if (!state || w.type === "button") return;
    if (w.titleEl && w.titleFn) w.titleEl.title = w.titleFn(state, this._t.bind(this)) || "";
    const focused = this.shadowRoot && this.shadowRoot.activeElement === w.el;
    if (w.type === "switch") {
      const shown = w.invert ? state.state !== "on" : state.state === "on";
      w.el.classList.toggle("on", shown);
      // gated feature switch: show/hide its sibling param rows when toggled
      if (w.gatedNodes) for (const n of w.gatedNodes) n.style.display = shown ? "" : "none";
    } else if (w.type === "select") {
      const opts = Array.isArray(state.attributes.options) ? state.attributes.options : [];
      const sig = opts.join("|");
      if (w.el.__opts !== sig) {
        w.el.__opts = sig;
        w.el.innerHTML = opts
          .map((o) => `<option value="${o}">${this._fmtOption(state, o)}</option>`)
          .join("");
      }
      if (!focused) w.el.value = state.state;
    } else if (w.type === "sensor") {
      const bad = state.state == null || state.state === "unknown" || state.state === "unavailable";
      w.val.textContent = bad
        ? "—"
        : (typeof this._hass.formatEntityState === "function"
          ? this._hass.formatEntityState(state)
          : state.state);
      const a = state.attributes || {};
      if (a.rms_error_w != null) {
        w.val.title = `RMS ${a.rms_error_w} W · ${a.oscillation_per_min ?? 0}/min`;
      }
    } else if (w.type === "number") {
      const a = state.attributes || {};
      const step = Number(a.step) || w.step || 1;
      if (a.step != null) w.el.step = a.step;
      // Floor min to a step boundary so the grid is absolute multiples of step
      // (matches HA's number slider, e.g. 12,15,20,…); commits clamp to real min.
      if (a.min != null) w.el.min = this._sliderMin(a.min, step);
      if (a.max != null) w.el.max = a.max;
      const unit = a.unit_of_measurement || w.unit || "";
      if (!this._rangePatchLocked(w.el, state, step)) {
        const v = Number(state.state);
        if (!Number.isNaN(v)) w.el.value = v;
        // Format the real state value, not w.el.value: a native range input snaps
        // its value to the min+k*step grid, so an off-grid state (e.g. 60 with
        // min 12 / step 5) would otherwise display as the snapped 62.
        w.val.textContent =
          state.state == null || state.state === "unknown" || state.state === "unavailable"
            ? "—"
            : this._fmtCtlNum(state.state, step, unit);
      }
    }
  }

  // Click/tap detail popover (works on touch, unlike hover `title`). One shared
  // node in the shadow root, repositioned per anchor; tap-again or tap-outside
  // closes it. Anchored under the label, flipped/clamped to stay on screen.
  _showInfoPopover(anchor, text) {
    if (!text) return;
    let pop = this._infoPop;
    if (!pop) {
      pop = document.createElement("div");
      pop.className = "info-pop";
      this.shadowRoot.appendChild(pop);
      this._infoPop = pop;
      this._infoPopDismiss = (ev) => {
        const p = this._infoPop;
        if (!p || !p._open) return;
        const t = ev.target;
        if (p.contains(t) || (p._anchor && p._anchor.contains(t))) return;
        this._hideInfoPopover();
      };
    }
    // second tap on the same anchor toggles it closed
    if (pop._open && pop._anchor === anchor) {
      this._hideInfoPopover();
      return;
    }
    pop._anchor = anchor;
    pop.textContent = text;
    pop.style.maxWidth = Math.min(340, window.innerWidth - 24) + "px";
    pop.style.display = "block";
    pop.style.left = "0px";
    pop.style.top = "0px";
    pop._open = true;
    const r = anchor.getBoundingClientRect();
    const pr = pop.getBoundingClientRect();
    let left = r.left;
    if (left + pr.width > window.innerWidth - 12) left = window.innerWidth - pr.width - 12;
    if (left < 12) left = 12;
    let top = r.bottom + 6;
    if (top + pr.height > window.innerHeight - 12) top = r.top - pr.height - 6;
    if (top < 12) top = 12;
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    // defer so the opening click doesn't immediately dismiss it
    setTimeout(() => window.addEventListener("click", this._infoPopDismiss, true), 0);
  }

  /** Append an info (ⓘ) button to a section card header carrying options-flow
   *  help: native title for desktop hover + tap popover for touch. No-op without
   *  text (sections lacking an options-flow description get no button). */
  _attachHelp(head, text) {
    if (!head || !text) return;
    const b = document.createElement("button");
    b.className = "card-info";
    b.setAttribute("aria-label", "info");
    b.title = text;
    b.innerHTML = `<ha-icon icon="mdi:information-outline"></ha-icon>`;
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      this._showInfoPopover(b, text);
    });
    head.appendChild(b);
  }

  _hideInfoPopover() {
    const pop = this._infoPop;
    if (!pop) return;
    pop.style.display = "none";
    pop._open = false;
    pop._anchor = null;
    window.removeEventListener("click", this._infoPopDismiss, true);
  }

  // Open Home Assistant's native more-info dialog for an entity (it includes the
  // history graph). Fired as a bubbling/composed event the HA frontend listens for.
  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(
      new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true })
    );
  }

  // Mark an element as a more-info trigger (cursor + tooltip + click). No-op when
  // the entity is absent, so missing sensors stay non-clickable.
  _linkMoreInfo(el, entityId) {
    if (!el || !entityId) return;
    el.classList.add("clickable");
    el.title = this._t("moreInfo");
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      this._moreInfo(entityId);
    });
  }

  // --- styles ----------------------------------------------------------------
  _styleEl() {
    const style = document.createElement("style");
    style.textContent = `
      :host {
        --accent-h: 155;
        --accent: oklch(0.78 0.15 var(--accent-h));
        --accent-soft: oklch(0.78 0.15 var(--accent-h) / 0.14);
        --accent-line: oklch(0.78 0.15 var(--accent-h) / 0.35);
        --accent-ink: oklch(0.22 0.04 var(--accent-h));
        --solar: oklch(0.84 0.15 88);
        --grid: oklch(0.72 0.12 268);
        --home: oklch(0.82 0.07 220);
        --flow-purple: oklch(0.50 0.27 295);
        --flow-orange: oklch(0.75 0.17 58);
        --flow-blue: oklch(0.70 0.15 245);
        --flow-green: oklch(0.78 0.16 150);
        --battery: var(--accent);
        --daily-op-solar-window: var(--solar);
        --daily-op-solar-charge: var(--flow-green);
        --daily-op-solar-line: var(--solar);
        --daily-op-grid: var(--flow-purple);
        --daily-op-hourly-balance: var(--flow-orange);
        --daily-op-discharge: var(--flow-blue);
        --daily-op-not-needed: var(--ink-dim);
        --daily-op-soc: oklch(0.74 0.18 330);
        --daily-op-delay: oklch(0.78 0.15 58);
        --font-ui: "Manrope", system-ui, sans-serif;
        --font-display: "Space Grotesk", system-ui, sans-serif;
        --gap: 18px; --pad: 22px; --radius: 20px; --radius-sm: 13px;
        display: block; height: 100%;
        font-family: var(--font-ui);
      }
      :host([data-theme="dark"]) {
        --bg-0: oklch(0.17 0.008 250); --bg-1: oklch(0.215 0.009 250);
        --bg-2: oklch(0.255 0.01 250); --bg-hover: oklch(0.30 0.012 250);
        --line: oklch(1 0 0 / 0.08); --line-strong: oklch(1 0 0 / 0.14);
        --ink: oklch(0.97 0.003 250); --ink-mid: oklch(0.74 0.008 250); --ink-dim: oklch(0.56 0.01 250);
        color-scheme: dark;
      }
      :host([data-theme="light"]) {
        --bg-0: oklch(0.965 0.004 250); --bg-1: oklch(0.995 0.002 250);
        --bg-2: oklch(0.975 0.003 250); --bg-hover: oklch(0.93 0.005 250);
        --line: oklch(0 0 0 / 0.09); --line-strong: oklch(0 0 0 / 0.16);
        --ink: oklch(0.25 0.01 250); --ink-mid: oklch(0.45 0.01 250); --ink-dim: oklch(0.6 0.01 250);
        color-scheme: light;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      .num { font-family: var(--font-display); font-feature-settings: "tnum" 1; letter-spacing: -0.01em; }
      .muted { color: var(--ink-mid); } .dim { color: var(--ink-dim); }
      ha-icon { display: inline-flex; }

      .app {
        display: flex; flex-direction: column; height: 100%;
        background: radial-gradient(120% 80% at 80% -10%, oklch(0.78 0.15 var(--accent-h) / 0.06), transparent 60%), var(--bg-0);
        color: var(--ink);
      }
      .appbar {
        display: flex; align-items: center; gap: 26px; height: 66px; padding: 0 30px; flex-shrink: 0;
        border-bottom: 1px solid var(--line);
        background: color-mix(in oklab, var(--bg-1) 80%, transparent);
        backdrop-filter: blur(10px);
      }
      .brand { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
      .brand .logo {
        width: 36px; height: 36px; border-radius: 11px; display: grid; place-items: center; cursor: pointer;
        background: var(--accent); color: var(--accent-ink);
        font-family: var(--font-display); font-weight: 700; font-size: 17px;
        box-shadow: 0 5px 16px oklch(0.78 0.15 var(--accent-h) / 0.4);
      }
      .brand .bt-name { font-family: var(--font-display); font-size: 15px; font-weight: 600; }
      .brand .bt-sub { font-size: 11px; color: var(--ink-dim); }

      .tabs { display: flex; align-items: stretch; gap: 2px; height: 100%; overflow-x: auto; scrollbar-width: none; }
      .tabs::-webkit-scrollbar { display: none; }
      .tab {
        display: flex; align-items: center; gap: 9px; padding: 0 17px; height: 100%;
        border: none; background: none; cursor: pointer; color: var(--ink-mid);
        font-family: var(--font-ui); font-size: 14px; font-weight: 600;
        border-bottom: 2.5px solid transparent; transition: color 0.16s; white-space: nowrap;
        --mdc-icon-size: 18px;
      }
      .tab:hover { color: var(--ink); }
      .tab.active { color: var(--accent); border-bottom-color: var(--accent); }


      .main { flex: 1; overflow-y: auto; padding: 26px 30px 44px; }
      .main::-webkit-scrollbar { width: 10px; }
      .main::-webkit-scrollbar-thumb { background: var(--line-strong); border-radius: 10px; border: 3px solid transparent; background-clip: content-box; }

      .pill { display: inline-flex; align-items: center; gap: 8px; padding: 9px 14px; border-radius: 999px;
        background: var(--bg-1); border: 1px solid var(--line); font-size: 13px; color: var(--ink-mid); }
      .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 10px var(--accent); }
      .pill .dot.live { animation: mvpulse 2.4s ease-in-out infinite; }
      @keyframes mvpulse { 0%,100%{opacity:1;} 50%{opacity:.35;} }

      .card { background: var(--bg-1); border: 1px solid var(--line); border-radius: var(--radius); padding: var(--pad); }
      .card-head { display: flex; align-items: center; gap: 9px; margin-bottom: 16px; --mdc-icon-size: 17px; }
      .card-head h2 { font-size: 13px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-mid); }
      .card-head .ic { color: var(--ink-dim); display: grid; place-items: center; }

      .res-stack { display: flex; flex-direction: column; gap: var(--gap); }
      /* lower row: Flujo (left) + 2×2 chart grid (right), equal height */
      .resumen-lower { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1.6fr); gap: var(--gap); align-items: stretch; }
      /* top row = "Energía hoy" content height (min-content): Energía semanal
         stretches to match it (its own chart min-content is shorter, so it never
         inflates the track), the bottom row fills the rest. The right column thus
         drives the block height and Flujo follows/crops to it (see .scene-stage). */
      .charts-2x2 { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: min-content minmax(0, 1fr); gap: var(--gap); min-width: 0; }
      .charts-2x2 > .card { min-width: 0; }
      /* Energía hoy is a fixed list of rows — keep it at content height instead of
         stretching to the (taller) Flujo column, which left empty space below it. */
      .charts-2x2 > .daily-card { align-self: start; }
      @media (max-width: 1080px) { .resumen-lower { grid-template-columns: 1fr; } }
      @media (max-width: 720px) { .charts-2x2 { grid-template-columns: 1fr; grid-template-rows: none; } }

      /* chart cards (Potencias / Energía semanal / SOC hoy) */
      .chart-card { display: flex; flex-direction: column; min-height: 0; }
      .chart-plot { flex: 1 1 auto; min-height: 96px; position: relative; }
      .chart-canvas { display: flex; height: 100%; min-height: 0; }
      .chart-yaxis { display: flex; flex: 0 0 48px; flex-direction: column; align-items: flex-end; justify-content: space-between; padding: 1px 8px 1px 0; color: var(--ink-dim); font-size: 10px; line-height: 1; white-space: nowrap; }
      .chart-yaxis small { margin-left: 2px; color: var(--ink-dim); font-size: 9px; }
      .chart-surface { position: relative; flex: 1 1 auto; min-width: 0; min-height: 0; }
      /* absolute so the SVG's intrinsic (viewBox) height never feeds back into the
         grid's min-content sizing — otherwise Energía semanal would inflate the
         shared top row past Energía hoy instead of matching it. */
      .chart-svg { display: block; position: absolute; inset: 0; width: 100%; height: 100%; }
      .chart-hover { position: absolute; inset: 0; pointer-events: none; z-index: 4; display: none; }
      .hv-line { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--line-strong); transform: translateX(-0.5px); }
      .hv-dot { position: absolute; width: 7px; height: 7px; border-radius: 50%; transform: translate(-50%, -50%); box-shadow: 0 0 0 2px var(--bg-1); }
      .hv-tip { position: absolute; top: 4px; padding: 6px 8px; border-radius: var(--radius-sm); background: var(--bg-2);
        border: 1px solid var(--line-strong); color: var(--ink); font-size: 11px; line-height: 1.4; white-space: nowrap;
        box-shadow: 0 6px 18px oklch(0 0 0 / 0.35); }
      .hv-tip .hv-h { font-weight: 600; margin-bottom: 3px; color: var(--ink-mid); }
      .hv-tip .hv-r { display: flex; justify-content: space-between; gap: 14px; }
      .hv-tip .hv-k { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-mid); }
      .hv-tip .hv-k i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; flex-shrink: 0; }
      .hv-tip .hv-v { font-variant-numeric: tabular-nums; color: var(--ink); }
      .chart-grid { stroke: var(--line); stroke-width: 1; vector-effect: non-scaling-stroke; }
      .chart-zero { stroke: var(--line-strong); stroke-width: 1; vector-effect: non-scaling-stroke; }
      .chart-xaxis { display: flex; justify-content: space-between; margin-top: 6px; padding-left: 48px; font-size: 11px; }
      .chart-legend { display: inline-flex; gap: 12px; flex-wrap: wrap; }
      .legend-item { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--ink-mid); }
      .legend-dot { width: 9px; height: 9px; border-radius: 2px; display: inline-block; flex-shrink: 0; }
      .chart-empty { display: grid; place-items: center; height: 100%; min-height: 96px; font-size: 12px; }
      /* zoom: range buttons under the chart + drag-to-brush selection box */
      .chart-zoom { display: flex; gap: 4px; justify-content: flex-end; margin-top: 6px; padding-left: 48px; }
      .zoom-btn { font-family: var(--font-ui); font-size: 11px; color: var(--ink-mid); background: var(--bg-2);
        border: 1px solid var(--line); border-radius: 7px; padding: 2px 8px; cursor: pointer; line-height: 1.5; }
      .zoom-btn:hover { background: var(--bg-hover); color: var(--ink); }
      .zoom-btn.active { background: var(--accent-soft); border-color: var(--accent-line); color: var(--accent); }
      .chart-plot, .mini-spark { touch-action: pan-y; }
      .brush-box { position: absolute; top: 0; bottom: 0; background: var(--accent-soft);
        border-left: 1px solid var(--accent-line); border-right: 1px solid var(--accent-line);
        pointer-events: none; z-index: 5; }

      /* ===== Daily operation timeline ===== */
      .daily-operation-card { position: relative; min-width: 0; overflow: visible; --daily-op-confirmed-shade-opacity: 50%; --daily-op-future-shade-opacity: 25%; --daily-op-shade-opacity: var(--daily-op-confirmed-shade-opacity); }
      .daily-operation-card[hidden], .daily-operation-card [hidden] { display: none !important; }
      .daily-op-description { font-size: 12px; line-height: 1.45; margin: -7px 0 12px; }
      .daily-op-toolbar { display: flex; align-items: center; gap: 12px; min-width: 0; margin-bottom: 9px; }
      .daily-op-legend { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
      .daily-op-legend-item { display: inline-flex; align-items: center; gap: 5px; color: var(--ink-mid); font-size: 11px; white-space: nowrap; }
      .daily-op-swatch { display: inline-block; width: 12px; height: 12px; border: 1px solid var(--line-strong); border-radius: 3px; }
      .daily-op-swatch-solar-window { background: color-mix(in oklab, var(--daily-op-solar-window) var(--daily-op-shade-opacity), transparent); }
      .daily-op-swatch-solar { background: color-mix(in oklab, var(--daily-op-solar-charge) var(--daily-op-shade-opacity), transparent); }
      .daily-op-swatch-grid { background: color-mix(in oklab, var(--daily-op-grid) var(--daily-op-shade-opacity), transparent); }
      .daily-op-swatch-hourly-balance { background: repeating-linear-gradient(135deg, transparent 0 3px, color-mix(in oklab, var(--daily-op-hourly-balance) var(--daily-op-shade-opacity), transparent) 3px 5px); border-color: var(--daily-op-hourly-balance); }
      .daily-op-swatch-discharge { background: color-mix(in oklab, var(--daily-op-discharge) var(--daily-op-shade-opacity), transparent); }
      .daily-op-swatch-not-needed { background: color-mix(in oklab, var(--daily-op-not-needed) var(--daily-op-shade-opacity), transparent); }
      .daily-op-line { display: inline-block; width: 15px; height: 0; border-top: 2px solid var(--ink); }
      .daily-op-line-solar { border-color: var(--daily-op-solar-line); }
      .daily-op-line-consumption { border-color: var(--home); border-top-style: dashed; }
      .daily-op-line-soc { border-color: var(--daily-op-soc); }
      .daily-op-nav { display: inline-flex; gap: 5px; margin-left: auto; flex-shrink: 0; }
      .daily-op-nav-btn { display: grid; place-items: center; width: 29px; height: 29px; padding: 0; border: 1px solid var(--line); border-radius: 8px; background: var(--bg-2); color: var(--ink-mid); cursor: pointer; --mdc-icon-size: 18px; }
      .daily-op-nav-btn:hover:not(:disabled) { color: var(--ink); background: var(--bg-hover); }
      .daily-op-nav-btn:disabled { opacity: .38; cursor: default; }
      .daily-op-badge { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; padding: 5px 9px; border: 1px solid var(--accent-line); border-radius: 999px; color: var(--accent); background: var(--accent-soft); font-size: 11px; font-weight: 600; white-space: nowrap; }
      .daily-op-badge-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
      .daily-op-badge-stale { color: oklch(0.82 0.14 75); border-color: oklch(0.82 0.14 75 / .4); background: oklch(0.82 0.14 75 / .1); }
      .daily-op-notice { margin: 0 0 9px; padding: 7px 10px; border: 1px solid oklch(0.82 0.14 75 / .35); border-radius: 9px; color: oklch(0.82 0.14 75); background: oklch(0.82 0.14 75 / .08); font-size: 11px; line-height: 1.4; }
      .daily-op-layout { display: grid; grid-template-columns: 62px minmax(0, 1fr) 42px; gap: 0; min-width: 0; }
      .daily-op-yaxis { position: relative; height: 190px; color: var(--ink-dim); font-size: 10px; line-height: 1; white-space: nowrap; }
      .daily-op-yaxis small { position: absolute; top: 7px; right: 8px; font-size: 9px; writing-mode: horizontal-tb; transform: none; }
      .daily-op-yaxis-ticks { position: absolute; top: 32px; right: 8px; bottom: 26px; display: flex; flex-direction: column; align-items: flex-end; justify-content: space-between; }
      .daily-op-soc-axis { position: relative; height: 190px; color: var(--ink-dim); font-size: 10px; line-height: 1; white-space: nowrap; }
      .daily-op-soc-axis small { position: absolute; top: 7px; left: 8px; font-size: 9px; }
      .daily-op-soc-axis-ticks { position: absolute; top: 32px; left: 8px; bottom: 26px; display: flex; flex-direction: column; align-items: flex-start; justify-content: space-between; }
      .daily-op-viewport { position: relative; min-width: 0; overflow-x: auto; overflow-y: hidden; scroll-snap-type: x proximity; scrollbar-width: thin; overscroll-behavior-x: contain; outline: none; }
      .daily-op-viewport:focus-visible { box-shadow: inset 0 0 0 2px var(--accent-line); border-radius: 8px; }
      .daily-op-stage { position: relative; width: 150%; min-width: 1440px; height: 190px; }
      .daily-op-svg { position: absolute; inset: 0; z-index: 1; display: block; width: 100%; height: 190px; overflow: visible; pointer-events: none; }
      .daily-op-grid-lines line { stroke: var(--line); stroke-width: 1; vector-effect: non-scaling-stroke; }
      .daily-op-grid-lines .daily-op-hour-line { stroke: var(--line-strong); }
      .daily-op-path { stroke-width: 2.2; vector-effect: non-scaling-stroke; stroke-linecap: round; stroke-linejoin: round; }
      .daily-op-path-solar-actual { stroke: var(--daily-op-solar-line); }
      .daily-op-path-solar-forecast { stroke: var(--daily-op-solar-line); stroke-dasharray: 5 4; opacity: .78; }
      .daily-op-path-consumption-actual { stroke: var(--home); }
      .daily-op-path-consumption-forecast { stroke: var(--home); stroke-dasharray: 5 4; opacity: .78; }
      .daily-op-path-soc-actual { stroke: var(--daily-op-soc); }
      .daily-op-path-soc-forecast { stroke: var(--daily-op-soc); stroke-dasharray: 5 4; opacity: .82; }
      .daily-op-grid { position: absolute; inset: 0; z-index: 2; display: grid; grid-template-rows: 24px 146px 20px; pointer-events: none; }
      .daily-op-now-marker { position: absolute; z-index: 5; top: 3px; bottom: 20px; width: 0; pointer-events: none; }
      .daily-op-now-marker::before { content: ""; position: absolute; top: 21px; bottom: 0; left: 0; border-left: 1.5px solid var(--accent); }
      .daily-op-now-marker::after { content: ""; position: absolute; top: 18px; left: -3px; width: 7px; height: 7px; border: 1px solid var(--bg-1); border-radius: 50%; background: var(--accent); }
      .daily-op-now-text { position: absolute; top: 0; left: 0; transform: translateX(-50%); padding: 2px 7px; border: 1px solid var(--accent-line); border-radius: 999px; background: var(--bg-2); color: var(--accent); box-shadow: 0 2px 8px oklch(0 0 0 / .22); font-size: 9px; line-height: 14px; font-weight: 700; letter-spacing: .01em; white-space: nowrap; }
      .daily-op-hour-labels, .daily-op-hours { display: grid; grid-template-columns: repeat(36, minmax(0, 1fr)); min-width: 0; }
      .daily-op-hour-label { padding-left: 4px; color: var(--ink-dim); font-size: 10px; font-variant-numeric: tabular-nums; }
      .daily-op-hour { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); min-width: 0; scroll-snap-align: start; border-right: 1px solid var(--line-strong); }
      .daily-op-cell { position: relative; display: block; min-width: 0; height: 146px; padding: 0; border: 0; border-left: 1px solid var(--line); border-radius: 0; background-color: transparent; background-repeat: repeat; background-size: 8px 8px; cursor: pointer; pointer-events: auto; transition: filter .12s, opacity .12s; }
      .daily-op-cell:hover, .daily-op-cell:focus-visible { z-index: 4; filter: brightness(1.18); outline: 2px solid var(--accent); outline-offset: -2px; }
      .daily-op-cell.daily-op-current { box-shadow: inset 0 0 0 1px var(--accent); }
      .daily-op-cell.daily-op-forecast { border-bottom: 1px dashed var(--line-strong); --daily-op-shade-opacity: var(--daily-op-future-shade-opacity); }
      .daily-op-cell.daily-op-stale { opacity: .55; }
      .daily-op-base-solar-window { background-color: color-mix(in oklab, var(--daily-op-solar-window) var(--daily-op-shade-opacity), transparent); }
      .daily-op-base-solar { background-color: color-mix(in oklab, var(--daily-op-solar-charge) var(--daily-op-shade-opacity), transparent); }
      .daily-op-base-grid { background-color: color-mix(in oklab, var(--daily-op-grid) var(--daily-op-shade-opacity), transparent); }
      .daily-op-base-discharge { background-color: color-mix(in oklab, var(--daily-op-discharge) var(--daily-op-shade-opacity), transparent); }
      .daily-op-base-not-needed { background-color: color-mix(in oklab, var(--daily-op-not-needed) var(--daily-op-shade-opacity), transparent); }
      .daily-op-base-neutral { background-color: color-mix(in oklab, var(--bg-2) var(--daily-op-shade-opacity), transparent); }
      .daily-op-cell.daily-op-hourly-balance::after { content: ""; position: absolute; left: 1px; right: 1px; bottom: 0; height: 3px; background: color-mix(in oklab, var(--daily-op-hourly-balance) var(--daily-op-shade-opacity), transparent); opacity: .9; }
      .daily-op-cell.daily-op-delay::before { content: ""; position: absolute; left: 1px; right: 1px; top: 0; height: 3px; background: var(--daily-op-delay); }
      .daily-op-delay-mark, .daily-op-setpoint-mark { position: absolute; z-index: 2; display: grid; place-items: center; color: var(--daily-op-delay); --mdc-icon-size: 12px; }
      .daily-op-delay-mark { top: 4px; right: 2px; }
      .daily-op-setpoint-mark { right: 2px; bottom: 3px; color: var(--ink-mid); opacity: .85; }
      .daily-op-dst-skipped { opacity: .25; background-image: repeating-linear-gradient(135deg, transparent 0 4px, var(--line-strong) 4px 5px) !important; }
      .daily-op-dst-repeated { box-shadow: inset 0 0 0 1px var(--ink-dim); }
      .daily-op-tooltip { position: fixed; z-index: 10; max-width: min(280px, calc(100vw - 16px)); min-width: min(190px, calc(100vw - 16px)); padding: 8px 10px; border: 1px solid var(--line-strong); border-radius: 10px; background: var(--bg-2); color: var(--ink); box-shadow: 0 8px 24px oklch(0 0 0 / .35); font-size: 11px; line-height: 1.35; pointer-events: none; }
      .daily-op-tip-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 5px; font-variant-numeric: tabular-nums; }
      .daily-op-tip-head strong { color: var(--ink); font-weight: 700; }
      .daily-op-tip-head span { color: var(--ink-dim); font-size: 10px; font-weight: 600; }
      .daily-op-tip-row { display: flex; justify-content: space-between; gap: 14px; border-top: 1px solid var(--line); padding-top: 3px; margin-top: 3px; }
      .daily-op-tip-row span { color: var(--ink-mid); }
      .daily-op-tip-row strong { color: var(--ink); text-align: right; font-weight: 600; font-variant-numeric: tabular-nums; }
      @media (max-width: 720px) {
        .daily-op-toolbar { align-items: flex-start; }
        .daily-op-legend { gap: 8px; }
        .daily-op-layout { grid-template-columns: 54px minmax(0, 1fr) 38px; }
        .daily-op-yaxis { padding-right: 6px; }
      }

      .stat-label { font-size: 12.5px; color: var(--ink-mid); font-weight: 600; display: flex; align-items: center; gap: 7px; --mdc-icon-size: 15px; }
      .stat-value { font-family: var(--font-display); font-weight: 600; letter-spacing: -0.02em; line-height: 1; font-size: 26px; }
      .stat-unit { color: var(--ink-dim); font-weight: 500; font-size: 0.5em; }

      /* flow — 3D-render scene with leader-line callouts */
      /* width-based square: fills the column width and stays square (never
         letterboxed). It anchors the block height; the 2×2 column matches it. */
      .flow-card { position: relative; overflow: hidden; }
      .flow-wrap { display: grid; place-items: center; }
      .scene-stage { position: relative; width: 100%; max-width: 540px; aspect-ratio: 1; margin: 0 auto; container-type: inline-size; }
      .scene-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; border-radius: 14px; user-select: none; -webkit-user-drag: none; }
      .lead-svg { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none; }
      .lead { fill: none; stroke: #8b9197; stroke-width: 0.4; opacity: 0.55; stroke-linecap: round; stroke-linejoin: round; transition: opacity 0.4s, stroke-width 0.4s; }
      .lead.on { opacity: 0.9; stroke-width: 0.5; }
      .lead-end { fill: #c8ccd0; opacity: 0.5; transition: opacity 0.4s; }
      .lead-end.on { opacity: 0.95; }
      /* animated "snake": one dash (15% of the path) travels the whole polyline.
         pathLength=100 normalizes geometry so dasharray is the same on every edge. */
      /* two long colored dashes per path: dasharray sums to 50 → repeats twice
         over pathLength=100, so exactly two segments travel each gray line. */
      /* NOTE: no vector-effect:non-scaling-stroke here — it makes dasharray use
         screen pixels and breaks the pathLength=100 normalization (the dashes
         turn into many short segments). Plain user-unit stroke keeps exactly two
         dashes per path. Width/glow are in viewBox units (~5.4x on screen). */
      .lead-flow { fill: none; stroke: currentColor; color: var(--home); stroke-width: 0.6;
        stroke-linecap: round; stroke-linejoin: round;
        stroke-dasharray: 38 12; stroke-dashoffset: 0; opacity: 0; pointer-events: none;
        transition: opacity 0.45s ease;
        filter: drop-shadow(0 0 0.7px currentColor) drop-shadow(0 0 1.8px currentColor); }
      .lead-flow.on { opacity: 0.95; animation: mv-snake 1.6s linear infinite; }
      /* distinct animation-name (not just animation-direction) so a live direction
         flip restarts the animation and actually reverses travel in Chrome.
         One pattern period is 50, so animate the offset by 50 for a seamless loop. */
      .lead-flow.on.rev { animation-name: mv-snake-rev; }
      @keyframes mv-snake { from { stroke-dashoffset: 0; } to { stroke-dashoffset: 50; } }
      @keyframes mv-snake-rev { from { stroke-dashoffset: 50; } to { stroke-dashoffset: 0; } }
      @media (prefers-reduced-motion: reduce) { .lead-flow.on { animation: none; opacity: 0.6; } }
      .scene-lbl { position: absolute; transform: translate(-50%, -50%); display: flex; flex-direction: column; align-items: center; gap: 1px; text-align: center; pointer-events: none; text-shadow: 0 1px 4px rgba(0,0,0,0.85); }
      .lbl-val { font-size: clamp(12px, 3.52cqw, 19px); font-weight: 700; color: #fff; line-height: 1; white-space: nowrap; }
      .lbl-val .fn-unit { font-size: 0.58em; font-weight: 600; color: rgba(255,255,255,0.7); margin-left: 2px; }
      .lbl-cap { font-size: clamp(7px, 1.67cqw, 9px); letter-spacing: 0.1em; text-transform: uppercase; color: rgba(255,255,255,0.55); font-weight: 600; margin-top: 2px; }
      .lbl-badge { font-size: clamp(7.5px, 1.85cqw, 10px); color: rgba(255,255,255,0.7); }
      .scene-lbl:not(.active) .lbl-val { color: rgba(255,255,255,0.78); }
      .scene-self { position: absolute; left: 50%; bottom: 3%; transform: translateX(-50%); font-size: clamp(8px, 2.04cqw, 11px); color: rgba(255,255,255,0.6); letter-spacing: 0.03em; pointer-events: none; text-shadow: 0 1px 4px rgba(0,0,0,0.85); }
      .scene-self .hub-self { color: var(--accent); font-weight: 700; }

      /* soc hero — ring (SOC + capacity + power) left, diagnostics 2 cols right */
      .soc-card { display: flex; flex-direction: column; gap: 18px; }
      .soc-card .card-head { align-self: stretch; margin-bottom: 4px; }
      .soc-inner { display: flex; gap: 30px; align-items: stretch; }
      .soc-left { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; flex: 0 0 auto; }
      .soc-diag { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; justify-content: center; border-left: 1px solid var(--line); padding-left: 30px; }
      .soc-diag-title { display: flex; align-items: center; gap: 9px; margin-bottom: 8px; font-size: 13px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-mid); --mdc-icon-size: 17px; }
      .soc-diag-title ha-icon { color: var(--ink-dim); }
      .diag-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 30px; }
      .diag-cell { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; padding: 9px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
      .diag-cell-label { color: var(--ink-mid); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .diag-cell .chip { flex-shrink: 0; max-width: 58%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      @media (max-width: 860px) {
        .soc-inner { flex-direction: column; align-items: center; gap: 20px; }
        .soc-diag { align-self: stretch; border-left: none; padding-left: 0; border-top: 1px solid var(--line); padding-top: 18px; }
      }
      @media (max-width: 560px) { .diag-grid { grid-template-columns: 1fr; } }
      .ring { position: relative; }
      /* let the SOC-color glow (drop-shadow) paint outside the svg box instead of being clipped */
      .ring svg { overflow: visible; }
      .ring .ring-fg { transition: stroke-dashoffset 0.8s cubic-bezier(.4,0,.2,1), stroke 0.6s ease; }
      .ring-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; gap: 2px; }
      .ring-val { font-size: 50px; font-weight: 600; line-height: 1; }
      .ring-val span { font-size: 0.42em; color: var(--ink-mid); }
      .ring-sub { font-size: 12px; }
      .soc-power { width: 100%; max-width: 300px; }
      .soc-power .pw-stats { display: flex; justify-content: space-between; gap: 16px; }
      .soc-power .stat-value { font-size: 23px; }
      .soc-power .pw-avail { font-size: 11px; margin-top: 6px; text-align: center; }

      /* chips */
      .chip { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; border: 1px solid var(--line); background: var(--bg-2); color: var(--ink-mid); }
      .chip-good { color: var(--accent); border-color: var(--accent-line); background: var(--accent-soft); }
      .chip-warn { color: oklch(0.82 0.14 75); border-color: oklch(0.82 0.14 75 / 0.35); background: oklch(0.82 0.14 75 / 0.12); }
      .chip-bad { color: oklch(0.7 0.18 25); border-color: oklch(0.7 0.18 25 / 0.4); background: oklch(0.7 0.18 25 / 0.12); }

      /* daily bars */
      .socbar { height: 8px; border-radius: 999px; background: var(--bg-2); overflow: hidden; }
      .socbar > span { display: block; height: 100%; border-radius: 999px; background: var(--battery); transition: width 0.8s cubic-bezier(.4,0,.2,1); }
      .daily-body { display: flex; flex-direction: column; gap: 10px; }
      .daily-row { display: flex; flex-direction: column; gap: 4px; }
      .daily-row-forecast { margin-top: 4px; padding-top: 10px; border-top: 1px solid var(--line); }
      .daily-row-forecast .socbar > span,
      .daily-row-remaining .socbar > span,
      .daily-row-expected .socbar > span { opacity: .62; }
      .daily-head { display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; }

      .mini-spark { margin-top: 2px; flex: 1 1 auto; min-height: 96px; }
      .mini-axis { display: flex; justify-content: space-between; margin-top: 6px; padding-left: 48px; font-size: 11px; }

      .placeholder { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; text-align: center; padding: 80px 20px; color: var(--ink-mid); --mdc-icon-size: 48px; }
      .placeholder ha-icon { color: var(--ink-dim); }
      .placeholder h3 { font-family: var(--font-display); font-size: 22px; color: var(--ink); }
      .placeholder p { max-width: 360px; font-size: 14px; }

      /* ===== Baterías tab ===== */
      .bat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(400px, 100%), 1fr)); gap: var(--gap); align-items: start; }
      .bat-card { display: flex; flex-direction: column; gap: 16px; min-width: 0; container: battery-card / inline-size; }
      .bat-head { display: flex; align-items: center; gap: 10px; }
      .bat-title { display: flex; align-items: center; gap: 9px; min-width: 0; flex: 1 1 auto; --mdc-icon-size: 18px; }
      .bat-title .ic { color: var(--ink-dim); display: grid; place-items: center; flex-shrink: 0; }
      .bat-name { font-family: var(--font-display); font-size: 15px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .bat-head .chip { flex-shrink: 0; }
      .bat-chips { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
      .bat-top { display: flex; align-items: center; gap: 18px; }
      .bat-ring { flex: 0 0 auto; }
      .bat-ring .ring-val { font-size: 30px; font-weight: 600; line-height: 1; }
      .bat-power { flex: 1 1 auto; min-width: 0; position: relative; }
      .bat-pwr { display: flex; align-items: baseline; gap: 1px; }
      .bat-pwr-val { font-family: var(--font-display); font-weight: 600; font-size: 26px; line-height: 1; letter-spacing: -0.02em; }
      .bat-pwr-unit { font-size: 13px; }
      .bat-pwr-lbl { font-size: 12px; margin-top: 3px; }
      .bat-pwr-track { width: 100%; }
      .bat-pwr-avail { font-size: 11px; margin-top: 4px; }
      .bat-cap { font-size: 12px; margin-top: 6px; }
      .bat-mppt-flows { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
      .bat-flow { min-width: 0; padding: 9px 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--bg-2); }
      .bat-flow-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; }
      .bat-flow-value { font-family: var(--font-display); font-size: 21px; font-weight: 600; line-height: 1.25; }
      .bat-flow-unit { font-size: 12px; }
      .bat-flow-sub { font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .bat-mppt-total { grid-column: 1 / -1; display: flex; align-items: baseline; justify-content: space-between; gap: 8px; padding: 0 2px; font-size: 12px; }
      .bat-mppt-total-val { font-size: 13px; }
      .bat-power.has-mppt .bat-cap { margin-top: 8px; }
      /* off-grid power: right edge, aligned with the AC-power line */
      .bat-offgrid { position: absolute; top: 0; right: 0; text-align: right; }
      .bat-offgrid .bat-pwr { justify-content: flex-end; }
      .bat-og-val { font-size: 20px; color: oklch(0.75 0.17 58); }
      .bat-og-unit { font-size: 12px; }
      .bat-og-lbl { font-size: 11px; margin-top: 3px; }
      .bat-power.has-mppt .bat-offgrid { position: static; margin-top: 8px; text-align: left; }
      .bat-power.has-mppt .bat-offgrid .bat-pwr { justify-content: flex-start; }
      .bat-sect { display: flex; flex-direction: column; gap: 9px; }
      .bat-sect-t { font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--ink-mid); }
      .bat-metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
      .metric { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-width: 0; padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 13px; }
      .metric .m-k { color: var(--ink-mid); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .metric .m-v { flex-shrink: 0; font-size: 14px; }
      /* clickable values open HA more-info (history graph) */
      .clickable { cursor: pointer; }
      .metric.clickable:hover .m-v { color: var(--accent); }
      .bat-pwr.clickable:hover .bat-pwr-val { color: var(--accent); }
      .bat-flow.clickable:hover .bat-flow-value, .bat-mppt-total.clickable:hover .bat-mppt-total-val { color: var(--accent); }
      .bat-cap.clickable:hover { color: var(--ink); }
      .ctl-val.ctl-sensor.clickable:hover { color: var(--accent); }
      .daily-row.clickable:hover .daily-head .muted { color: var(--ink); }
      .ring.clickable:hover { filter: brightness(1.08); }
      .ring-sub.clickable:hover { color: var(--ink); }
      .statblock.clickable:hover .stat-value { filter: brightness(1.12); }
      .scene-lbl.clickable { pointer-events: auto; }
      .scene-lbl.clickable:hover .lbl-val { filter: brightness(1.15); }
      .diag-cell.clickable:hover .diag-cell-label { color: var(--ink); }
      .bat-mppt-chips { display: flex; flex-wrap: wrap; gap: 7px; }
      .mppt-chip { font-size: 11.5px; }
      .bat-info { border-top: 1px solid var(--line); padding-top: 10px; }
      .bat-info > summary { cursor: pointer; font-size: 12px; color: var(--ink-mid); font-weight: 600; list-style: none; display: flex; align-items: center; gap: 7px; }
      .bat-info > summary::-webkit-details-marker { display: none; }
      .bat-info > summary::before { content: "▸"; color: var(--ink-dim); transition: transform 0.2s; }
      .bat-info[open] > summary::before { transform: rotate(90deg); }
      .bat-info-grid { display: flex; flex-direction: column; gap: 5px; margin-top: 10px; }
      .info-row { display: flex; justify-content: space-between; gap: 12px; font-size: 12.5px; }
      .info-row span:first-child { white-space: nowrap; }
      .info-row span:last-child { font-variant-numeric: tabular-nums; color: var(--ink); text-align: right; word-break: break-all; }
      .bat-info > summary ha-icon { color: var(--ink-dim); --mdc-icon-size: 16px; }
      .m-tag { font-size: 9px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink-dim); background: var(--bg-2); border: 1px solid var(--line); border-radius: 5px; padding: 1px 4px; margin-left: 5px; vertical-align: middle; font-family: var(--font-ui); }

      /* per-battery controls — 2-col grid so labels and controls align across
         rows and every slider/select gets the same width */
      .bat-ctl-grid { display: grid; grid-template-columns: minmax(0, min(52%, 260px)) minmax(140px, 1fr); gap: 12px 14px; align-items: center; margin-top: 12px; }
      .ctl-k { display: inline-flex; align-items: center; gap: 7px; color: var(--ink-mid); font-size: 13px; --mdc-icon-size: 16px; white-space: nowrap; }
      .bat-ctl-grid .ctl-k { min-width: 0; white-space: normal; overflow-wrap: anywhere; }
      .ctl-k ha-icon { color: var(--ink-dim); flex-shrink: 0; }
      .ctl-empty { grid-column: 1 / -1; font-size: 12px; line-height: 1.45; }
      .ctl-toggle { justify-self: start; position: relative; width: 40px; height: 22px; border-radius: 999px; border: 1px solid var(--line-strong); background: var(--bg-2); cursor: pointer; padding: 0; transition: background 0.2s, border-color 0.2s; }
      .ctl-toggle .ctl-knob { position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: var(--ink-dim); transition: transform 0.2s, background 0.2s; }
      .ctl-toggle.on { background: var(--accent-soft); border-color: var(--accent-line); }
      .ctl-toggle.on .ctl-knob { transform: translateX(18px); background: var(--accent); }
      .ctl-select { width: 100%; font-family: var(--font-ui); font-size: 13px; color: var(--ink); background: var(--bg-2); border: 1px solid var(--line-strong); border-radius: 9px; padding: 5px 8px; cursor: pointer; }
      .ctl-num { display: flex; align-items: center; gap: 10px; width: 100%; min-width: 0; }
      .ctl-num input[type="range"] { flex: 1 1 auto; accent-color: var(--accent); cursor: pointer; min-width: 0; }
      .ctl-num .ctl-val { font-family: var(--font-display); font-variant-numeric: tabular-nums; font-size: 13px; color: var(--ink); white-space: nowrap; min-width: 56px; text-align: right; }
      .ctl-btn { grid-column: 1 / -1; display: inline-flex; align-items: center; justify-content: center; gap: 7px; width: 100%; padding: 8px 12px; border-radius: 11px; border: 1px solid var(--line-strong); background: var(--bg-2); color: var(--ink-mid); font-family: var(--font-ui); font-weight: 600; font-size: 13px; cursor: pointer; --mdc-icon-size: 16px; transition: background 0.15s, color 0.15s; }
      .ctl-btn:hover { background: var(--bg-hover); color: var(--ink); }
      @container battery-card (max-width: 380px) {
        .bat-ctl-grid { grid-template-columns: minmax(0, 1fr); row-gap: 7px; }
        .bat-ctl-grid > .ctl-k:not(:first-child) { margin-top: 6px; }
        .bat-ctl-grid > .ctl-toggle { margin-bottom: 3px; }
        .bat-ctl-grid > .ctl-btn { grid-column: 1; }
      }
      @media (max-width: 480px) { .bat-grid { grid-template-columns: 1fr; } }

      /* ===== Control tab ===== */
      /* Flattened layout: every feature card is an independent box in a single
         responsive grid (tracks size themselves via auto-fit, so columns
         appear/collapse with width; dense backfills empty trailing cells). Cards
         can be drag-reordered in arrange mode; order persists in localStorage. */
      .ctl-root { display: flex; flex-direction: column; gap: var(--gap); }
      .ctl-bar { display: flex; align-items: center; gap: 10px; }
      .ctl-hint { margin-right: auto; color: var(--ink-dim); font-size: 12px; }
      .ctl-arrange-btn { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px;
        border: 1px solid var(--line); border-radius: var(--radius-sm); background: var(--bg-1);
        color: var(--ink-mid); cursor: pointer; font-size: 13px; --mdc-icon-size: 16px; }
      .ctl-arrange-btn:hover { color: var(--ink); }
      .ctl-arrange-btn.active { color: var(--accent); border-color: var(--accent); }
      /* column/row steppers (arrange mode only): pin a fixed grid shape */
      .ctl-tools { display: inline-flex; align-items: center; gap: 16px; }
      .ctl-cols { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-mid); font-size: 13px; }
      .ctl-cols-lbl { color: var(--ink-dim); }
      .ctl-cols button { display: inline-flex; align-items: center; justify-content: center;
        width: 26px; height: 26px; border: 1px solid var(--line); border-radius: var(--radius-sm);
        background: var(--bg-1); color: var(--ink-mid); cursor: pointer; --mdc-icon-size: 16px; }
      .ctl-cols button:hover { color: var(--ink); }
      .ctl-cols-val { min-width: 2.5ch; text-align: center; font-variant-numeric: tabular-nums; }
      .sys-stack { display: grid; gap: var(--gap); align-items: start;
        grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
        grid-auto-flow: row dense; }
      /* let cards shrink to their track (the old .sys-col grid item carried this);
         without it long-label cards (PD/Common) stay at min-content and the inner
         2-col grid collapses the slider column to just the thumb. Each card is
         also a size container: its controls respond to the width the auto grid
         actually gives it (including HA's sidebar), not to the viewport. */
      .sys-stack .card { min-width: 0; container: sys-control-card / inline-size; }
      .sys-stack > .placeholder { grid-column: 1 / -1; }
      .sys-stack .card-head { margin-bottom: 0; }
      /* arrange mode: cards become grabbable, inner controls are locked so a drag
         never grabs a slider; the card being dragged dims */
      .sys-stack.arranging .card { cursor: grab; border-style: dashed; }
      .sys-stack.arranging .card:active { cursor: grabbing; }
      .sys-stack.arranging .card .bat-ctl-grid { pointer-events: none; }
      .sys-stack .card.dragging { opacity: 0.4; }
      /* manual matrix: cards sit inside fixed cells that are themselves drop
         targets. min-width:0 (the > .card rule above can't reach them now) keeps
         the inner slider grid from collapsing; empty cells show as dashed slots
         while arranging so it's clear where a card can be dropped. */
      .ctl-cell { min-width: 0; display: flex; }
      .ctl-cell > .card { width: 100%; min-width: 0; }
      .sys-stack.matrix.arranging .ctl-cell:empty { min-height: 64px;
        border: 1px dashed var(--line); border-radius: var(--radius-sm); }
      .ctl-cell.drop-target { outline: 2px solid var(--accent); outline-offset: -2px;
        border-radius: var(--radius-sm); }
      /* hide/show eye toggle: only shown while arranging */
      .ctl-hide-btn { display: none; margin-left: auto; padding: 0; border: 0; background: none;
        cursor: pointer; color: var(--ink-dim); place-items: center; --mdc-icon-size: 16px; }
      .ctl-hide-btn:hover { color: var(--ink); }
      .ctl-root.arranging .ctl-hide-btn { display: grid; }
      .card-head .card-info + .ctl-hide-btn, .card-head .ctl-hide-btn + .card-info { margin-left: 8px; }
      /* hidden-cards section: only visible while arranging; cards are parked
         (dimmed, controls locked) until the eye toggle restores them */
      .ctl-hidden { display: none; }
      .ctl-root.arranging .ctl-hidden { display: flex; flex-direction: column; gap: 10px; }
      .ctl-hidden-title { display: inline-flex; align-items: center; gap: 6px;
        color: var(--ink-dim); font-size: 12px; --mdc-icon-size: 16px; }
      .ctl-hidden-stack .card { border-style: dashed; opacity: 0.7; }
      .ctl-hidden-stack .card .bat-ctl-grid { pointer-events: none; }
      /* options-flow help affordance pinned to the right of a section header */
      .card-info { margin-left: auto; padding: 0; border: 0; background: none; cursor: pointer;
        color: var(--ink-dim); display: grid; place-items: center; --mdc-icon-size: 16px; }
      .card-info:hover { color: var(--ink); }
      /* narrow paired-column cards: let the label track shrink (max-content can't)
         and wrap, so sliders/buttons never overflow the card box at ~1080p */
      .sys-grid { margin-top: 14px; grid-template-columns: minmax(0, max-content) minmax(0, 1fr); }
      .sys-grid .ctl-k { white-space: normal; overflow-wrap: anywhere; }
      /* Auto-fit can legitimately make a card ~300px wide. At that point a
         label/value pair leaves too little room for a usable range input. Stack
         each pair inside the card instead; container queries keep this correct
         for viewport, sidebar and user-pinned layouts alike. */
      @container sys-control-card (max-width: 380px) {
        .sys-grid { grid-template-columns: minmax(0, 1fr); row-gap: 6px; }
        .sys-grid > .ctl-k:not(:first-child) { margin-top: 8px; }
        .sys-grid > .ctl-toggle { margin-bottom: 4px; }
        .sys-grid > .ctl-btn { grid-column: 1; }
      }
      /* label with a tap/hover detail popover (e.g. time-slot details) */
      .ctl-k-info { cursor: pointer; }
      .ctl-k-info > span { text-decoration: underline dotted var(--ink-dim); text-underline-offset: 3px; }
      .info-pop { position: fixed; z-index: 60; display: none; max-width: 340px; padding: 10px 12px;
        border-radius: var(--radius-sm); background: var(--bg-2); border: 1px solid var(--line-strong);
        color: var(--ink); font-family: var(--font-ui); font-size: 12px; line-height: 1.5; white-space: pre-line;
        box-shadow: 0 8px 24px oklch(0 0 0 / 0.4); }

      @media (max-width: 720px) {
        .appbar { padding: 0 14px; gap: 14px; height: 60px; }
        .brand .btext { display: none; }
        .tab { padding: 0 12px; }
        .tab .tab-label { display: none; }
        .main { padding: 18px 14px 32px; }
      }
    `;
    return style;
  }
}

if (!customElements.get("marstek-venus-panel")) {
  customElements.define("marstek-venus-panel", MarstekVenusPanel);
}
