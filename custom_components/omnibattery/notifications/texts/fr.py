"""Textes de notification en français."""

# Lundi en premier, comme ``datetime.weekday()``.
DAYS = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")

TEXTS: dict[str, str | dict[str, str]] = {
    # --- plan.py: _forecast_notification_context (horizon, labels, basis) ---
    "plan.horizon.until_tomorrow_midnight": "jusqu'à minuit demain",
    "plan.horizon.until_day_time": "jusqu'à {day} {time}",
    "plan.horizon.until_sunrise": "jusqu'au lever du soleil ({time})",
    "plan.horizon.until_midnight": "jusqu'à minuit",
    "plan.phrase.tomorrow": "le reste d'aujourd'hui et demain",
    "plan.phrase.tomorrow_until": "le reste d'aujourd'hui et demain ({horizon})",
    "plan.phrase.tonight": "le reste d'aujourd'hui et cette nuit {horizon}",
    "plan.phrase.today": "le reste d'aujourd'hui",
    "plan.basis.profile": "profil appris sur 15 minutes",
    "plan.basis.profile_days": "profil appris sur 15 minutes ({days} jours)",
    "plan.basis.temporary_curve": "courbe journalière temporaire",
    "plan.basis.remaining_average": "estimation du reste de la journée d'après la moyenne journalière sur {days} jours",
    "plan.basis.remaining_default": "estimation par défaut du reste de la journée",
    "plan.basis.average": "moyenne journalière sur {days} jours",
    "plan.basis.daily_default": "estimation journalière par défaut",
    "plan.basis.adjusted": "{basis}, ajusté avec la consommation d'aujourd'hui",
    "plan.balance.today": "Aujourd'hui",
    "plan.balance.tomorrow": "Demain",
    "plan.balance.night": "Nuit",
    "plan.balance.total": "Total",
    "plan.balance.solar": "☀️ Solaire prévu",
    "plan.balance.solar_margin": "☀️ Avec marge ({pct:.0f} %)",
    "plan.balance.consumption": "📊 Consommation maison",
    "plan.balance.battery": "🔋 Batterie ({soc:.0f} %)",
    "plan.balance.grid_charge": "🔌 Charge réseau",
    "plan.balance.available": "✅ Disponible (batterie + solaire)",
    "plan.balance.deficit": "⚡ Déficit d'énergie",
    "plan.balance.grid_need": "⚡ Besoin total du réseau",
    "plan.balance.grid_floor": "🔌 Charge réseau (SOC minimum garanti)",
    "plan.balance.solar_surplus": "☀️ Excédent solaire après consommation",
    "plan.balance.import_left": "🔌 Laissé au réseau",
    # --- plan.py: _build_forecast_lines and shared lines ---
    "plan.line.basis": "   🧮 Base : {basis}",
    "plan.battery": "🔋 Batterie : {soc:.0f}% ({usable:.2f} kWh utilisables)",
    "plan.horizon_text.today": "pour aujourd'hui",
    "plan.horizon_text.for": "pour {phrase}",
    "plan.sufficient": "✓ Énergie suffisante {horizon}",
    "plan.no_more_charging": "Aucune charge réseau supplémentaire nécessaire.",
    "plan.no_charging": "Aucune charge réseau nécessaire.",
    # --- plan.py: _limits_table (the power limits the plan used) ---
    "plan.power.header": "⚡ Puissance maximale",
    "plan.power.charge": "🔌 Charge",
    "plan.power.discharge": "🔋 Décharge",
    "plan.power.contracted": "{power:g}W (Import réseau max.)",
    "plan.power.batteries": "{power:g}W (batteries)",
    "plan.power.carried": (
        "La charge au-dessus de la puissance de décharge max. vient du réseau : {carried:.2f} kWh, exclue de la charge"
    ),
    # --- plan.py: format_predictive_notification_message ---
    "plan.safe.title": "Charge prédictive : mode sécurité",
    "plan.safe.no_forecast": "⚠️ Aucune prévision solaire disponible — mode prudent",
    "plan.safe.not_activated": "Charge réseau NON activée.",
    "plan.deferred.title": "Charge prédictive : différée selon le calendrier",
    "plan.deferred.no_quota": "🕒 Ce créneau configuré n'a pas de quota d'énergie.",
    "plan.deferred.shortfall": "⚠️ Manque avant l'échéance : {kwh:.2f} kWh",
    "plan.deferred.reserved": "L'énergie est réservée à un autre créneau configuré.",
    "plan.not_required.title": "Charge prédictive : non nécessaire",
    "plan.expected_today.title": "Charge prédictive : prévue aujourd'hui",
    "plan.started.title": "Charge prédictive : DÉMARRÉE",
    "plan.timing.when_cheap": "⏰ La charge s'activera lorsque les prix seront bas",
    "plan.timing.until": "⏰ Charge jusqu'à : {time}",
    "plan.timing.now": "⏰ Charge depuis le réseau en cours",
    "plan.charge.needed": "⚡ Déficit d'énergie — charge réseau nécessaire",
    # --- plan.py: day prefix, clock and span ---
    "plan.day_prefix": "{day} {date} · ",
    # --- plan.py: _plan_order_lines ---
    # The one table's title, then its legend (under the table); plan.py
    # puts each row's icon in front.
    "plan.orders.title": "🗓️ Plan par créneau :",
    "plan.orders.legend": "Légende :",
    "plan.orders.charge": "Charge réseau",
    "plan.orders.holds": "Batterie conservée pour plus tard (la maison achète au réseau)",
    "plan.orders.empty": "Batterie vide (la maison achète au réseau)",
    "plan.orders.exports": "☀️ Surplus solaire à exporter : {items}",
    "plan.orders.sales": "💶 Vente au réseau :",
    "plan.orders.room": "🔋 On fait de la place pour le soleil :",
    "plan.orders.solar_delay": "⏳ Charge solaire retardée par le plan",
    # --- plan.py: _no_slots_explanation ---
    "plan.noslots.awaiting_prices.headline": (
        "Charge nécessaire plus tard, mais ses prix ne sont pas encore publiés"
    ),
    "plan.noslots.awaiting_prices.hint": (
        "Les {kwh:.2f} kWh sont nécessaires après le dernier prix publié, donc aucun "
        "créneau ne peut encore être évalué. Le plan est recalculé dès que les prix "
        "du jour suivant sont publiés."
    ),
    "plan.noslots.cheaper_to_import.headline": (
        "Charge nécessaire, mais charger depuis le réseau maintenant n'est pas rentable"
    ),
    "plan.noslots.cheaper_to_import.hint": (
        "Importer les {kwh:.2f} kWh au moment de leur utilisation coûte moins cher que "
        "de les stocker maintenant, compte tenu des pertes aller-retour et de la marge "
        "d'arbitrage. Baissez la marge d'arbitrage minimale pour charger quand même."
    ),
    "plan.noslots.capacity.headline": (
        "Charge nécessaire, mais les créneaux restants ne permettent pas de la fournir à temps"
    ),
    "plan.noslots.capacity.hint": (
        "Les créneaux de prix restants avant le besoin d'énergie sont trop peu nombreux "
        "ou trop courts avec la puissance de charge disponible. Vérifiez la puissance "
        "de charge max. et la puissance contractée, ou attendez le prochain jour de prix."
    ),
    "plan.noslots.no_power.headline": (
        "Charge nécessaire, mais la puissance de charge disponible est nulle"
    ),
    "plan.noslots.no_power.hint": (
        "Vérifiez la puissance de charge max., la puissance contractée et la limite d'écrêtement des pointes."
    ),
    "plan.noslots.no_room.headline": "Charge nécessaire plus tard, mais rien ne peut être stocké maintenant",
    "plan.noslots.no_room.hint": (
        "La batterie n'a pas de place pour l'énergie avant son utilisation, donc aucun "
        "créneau n'est réservé. Le plan est recalculé à mesure que la batterie se vide "
        "et lors de la prochaine évaluation quotidienne."
    ),
    "plan.noslots.no_eligible.headline": (
        "Charge nécessaire, mais il ne reste aucun créneau de prix dans l'horizon de planification"
    ),
    "plan.noslots.no_eligible.hint": (
        "Vérifiez le capteur de prix : les prix du jour suivant ne sont peut-être pas encore publiés."
    ),
    "plan.noslots.default.headline": "Charge nécessaire, mais aucun créneau de prix valide trouvé",
    "plan.noslots.hint.arbitrage": (
        "L'écart de prix d'aujourd'hui est trop faible pour couvrir les pertes "
        "aller-retour avec la marge d'arbitrage configurée."
    ),
    "plan.noslots.hint.price": "Vérifiez le capteur de prix ou relevez le plafond de prix max.",
    "plan.noslots.cheapest": "Créneau le moins cher : {start} à {price:.4f} {unit}.\n{hint}",
    "plan.noslots.cheapest_ceiling": (
        "Créneau le moins cher : {start} à {price:.4f} {unit} "
        "(plafond {ceiling:.4f} {unit}).\n{hint}"
    ),
    # --- plan.py: _plan_order_lines, its last line ---
    "plan.savings": (
        "💰 Économie estimée du plan : ~{saving:.2f} {cost_unit} (par rapport à la batterie sans plan)"
    ),
    # --- plan.py: format_dynamic_pricing_notification ---
    "plan.price.charge": "charge ≤ {price:.4f} {unit}",
    "plan.price.discharge": "décharge ≥ {price:.4f} {unit}",
    "plan.price.arbitrage": "plafond d'arbitrage ≤ {price:.4f} {unit}",
    "plan.price.arbitrage_not_binding": (
        "plafond d'arbitrage ≤ {price:.4f} {unit} (non contraignant)"
    ),
    "plan.price.thresholds": "⚙️ Seuils de prix : {parts}",
    "plan.pricing.not_needed.title": "Charge prédictive : optimisation du prix - NON nécessaire",
    "plan.pricing.deferred.title": (
        "Charge prédictive : optimisation du prix - rien à charger pour l'instant"
    ),
    "plan.pricing.deferred.headline": "✓ Les créneaux les moins chers pour l'énergie nécessaire sont demain",
    "plan.pricing.deferred.footer": (
        "Aucune charge réseau n'est prévue maintenant ; elle est replanifiée lors de la prochaine évaluation quotidienne."
    ),
    "plan.pricing.import.title": "Charge prédictive : optimisation du prix - L'import réseau est moins cher",
    "plan.pricing.awaiting.title": "Charge prédictive : optimisation du prix - En attente des prix",
    "plan.pricing.no_slots.title": "Charge prédictive : optimisation du prix - Aucun créneau disponible",
    "plan.pricing.import.headline": (
        "ℹ️ Pas de charge réseau : acheter l'énergie au moment de son utilisation coûte moins cher"
    ),
    "plan.pricing.import.storing": (
        "La stocker maintenant coûterait plus cher après les pertes aller-retour."
    ),
    "plan.pricing.import.storing_margin": (
        "La stocker maintenant coûterait plus cher après les pertes aller-retour et la "
        "marge d'arbitrage de {margin:.4f} {unit}."
    ),
    "plan.pricing.warning": "⚠️ {headline}",
    "plan.pricing.hours": "{hours:g}h",
    "plan.pricing.hours_slots": "{hours:g}h ({slots})",
    "plan.pricing.slots": {"one": "{count} créneau", "other": "{count} créneaux"},
    "plan.pricing.selected.title": "Charge prédictive : optimisation du prix - {hours} sélectionnées",
    "plan.pricing.floor.title": (
        "Charge prédictive : optimisation du prix + SOC minimum garanti - {hours} sélectionnées"
    ),
    "plan.pricing.negative.title": "Charge à prix négatif : {hours} sélectionnées",
    "plan.pricing.negative.opportunistic": "⚡ Charge opportuniste prévue : {kwh:.2f} kWh",
    "plan.pricing.negative.no_deficit": "Aucun déficit d'énergie prédictif à couvrir.",
    "plan.pricing.negative.header": "💰 Créneaux qualifiés les plus négatifs :",
    "plan.pricing.info.title": "Charge prédictive : infos prix - {hours} les moins chères",
    "plan.pricing.info.no_charging": "✓ Aucune charge réseau nécessaire {horizon}",
    "plan.pricing.info.header": "💰 Heures les moins chères aujourd'hui (à titre informatif) :",
    "plan.pricing.info.footer": "Aucune charge ne s'activera.",
    "plan.pricing.opportunity": "⚡ Opportunité de prix négatif : {kwh:.2f} kWh",
    "plan.pricing.deadline": "🕒 Requis avant les échéances : {kwh:.2f} kWh",
    "plan.pricing.flexible": "💰 Flexible selon le prix : {kwh:.2f} kWh",
    "plan.pricing.tomorrow": (
        "📅 Planifié dans les créneaux de demain (programmé à 00:05) : {kwh:.2f} kWh"
    ),
    "plan.pricing.import_left": (
        "🔌 Laissé au réseau (import moins cher à l'utilisation) : {kwh:.2f} kWh"
    ),
    "plan.pricing.cannot_deliver": (
        "⚠️ Impossible de fournir {kwh:.2f} kWh avant son échéance ({reason})."
    ),
    "plan.pricing.selected_deadline": "Créneaux sélectionnés (les moins chers respectant chaque échéance)",
    "plan.pricing.selected_hours": "Heures sélectionnées (les moins chères)",
    "plan.pricing.grid_charge_hours": (
        "🔌 Charge réseau prévue : {kwh:.2f} kWh → {hours:.1f}h de charge nécessaires"
    ),
    "plan.pricing.slots_header": "💰 {label} :",
    "plan.pricing.average_price": "Prix moyen : {price:.4f} {unit}",
    "plan.pricing.estimated_cost": "Coût estimé : ~{cost:.2f} {cost_unit}",
    # Pieces of one slot line (a price slot row); `extras` joins the optional ones.
    "plan.slot.line": "  • {span} → {price:.4f} {unit}{extras}",
    "plan.slot.kwh": ", {kwh:.2f} kWh",
    "plan.slot.before": " [avant {time}]",
    "plan.slot.flexible": " [flexible]",
    "plan.slot.purpose": " [{purpose}]",
    "plan.slot.purpose_extra": " + {purpose}",
    "plan.table.header": "| Créneau | Prix | Énergie | Coût |",
    "plan.table.power": "Puissance",
    "plan.table.price": "{price:.4f} {unit}",
    "plan.table.cost": "~{cost:.2f} {cost_unit}",
    "plan.table.total": "**Total**",
    "plan.table.kwh": "{kwh:.2f} kWh",
    # --- plan.py: format_slot_start_notification ---
    "slot_start.title": "Charge prédictive : DÉMARRÉE ({price:.4f} {unit})",
    "slot_start.charging": "⚡ Charge à {power:g}W max.",
    "slot_start.slot": "Créneau : {span}",
    # English keeps the phase 3 text ("slot(s)"); other languages use their plural.
    "slot_start.next": {
        "one": "Prochain créneau : {time} · {count} créneau restant",
        "other": "Prochain créneau : {time} · {count} créneaux restants",
    },
    "slot_start.last": "Dernier créneau · Plus de créneaux aujourd'hui",
    # --- plan.py: format_dp_pre_slot_reevaluation_notification ---
    "pre_slot.title": "Charge prédictive : créneau {time} confirmé — charge nécessaire",
    "pre_slot.slot": "Créneau : {span} @ {price:.4f} {unit}",
    "pre_slot.activate": "→ La charge s'activera à {time}",
    # --- switch.py: predictive charging override and manual mode ---
    "switch.override.title": "Charge prédictive : désactivée",
    "switch.override.paused": (
        "La charge réseau prédictive est en pause. Réactivez l'interrupteur pour reprendre."
    ),
    "switch.override.disabled": (
        "La charge prédictive est maintenant désactivée. Elle ne s'activera pas "
        "lorsque le créneau horaire deviendra actif."
    ),
    "switch.manual.title": "Mode manuel : actif",
    "switch.manual.message": (
        "Le contrôle automatique de la charge/décharge est en pause. "
        "Toutes les batteries ont été mises au repos (0W). "
        "Vous pouvez maintenant piloter chaque batterie manuellement avec les "
        "commandes « Puissance de charge/décharge forcée », ou — "
        "pour les batteries dont l'intégration propre offre davantage "
        "de modes de fonctionnement — y choisir un autre mode "
        "sans qu'OmniBattery le rétablisse.\n\n"
        "Désactivez le mode manuel pour reprendre le contrôle automatique."
    ),
    # --- infra/alarm_notifier.py: AlarmNotifier._send (driver alarm labels stay English) ---
    "alarm.title.fault": "🚨 Défaut de batterie : {device}",
    "alarm.title.warning": "⚠️ Avertissement de batterie : {device}",
    "alarm.header.fault": "🚨 Défaut détecté sur {device}",
    "alarm.header.warning": "⚠️ Avertissement détecté sur {device}",
    "alarm.new_faults": "🆕 Nouveaux défauts :",
    "alarm.new_alarms": "🆕 Nouvelles alarmes :",
    "alarm.also_active": "⚡ Également actives :",
    # --- tracking/balance_monitor.py: title and `issues` lines ---
    "balance.title": "{icon} Équilibrage des cellules : {name}",
    "balance.high": "Delta : {delta:.0f} mV — fort déséquilibre des cellules.",
    "balance.moderate": "Delta : {delta:.0f} mV — déséquilibre modéré.",
    "balance.consecutive": (
        "Fort déséquilibre des cellules ({delta:.0f} mV) pendant {count} charges "
        "complètes consécutives. Envisagez d'utiliser un blueprint d'équilibrage "
        "actif pour cette batterie afin de rééquilibrer les cellules."
    ),
    "balance.trend": (
        "Tendance à la hausse du déséquilibre : +{slope:.1f} mV/mesure, "
        "moy. {avg:.0f} mV sur les dernières mesures."
    ),
    # --- control/weekly_full_charge.py: solar outlook warning ---
    "weekly.title": "Charge complète hebdomadaire : pas assez de solaire",
    "weekly.not_enough_solar": (
        "Le solaire restant aujourd'hui ({net:.1f} kWh après la consommation de la maison) "
        "ne couvrira pas les {needed:.1f} kWh encore nécessaires pour atteindre 100%. "
        "Avec la charge prédictive désactivée, la charge complète hebdomadaire ne "
        "charge pas depuis le réseau. Activez la charge prédictive, ou laissez une "
        "automatisation agir sur l'attribut solar_sufficient du capteur de charge "
        "complète hebdomadaire."
    ),
    # --- __init__.py: legacy active-balance migration ---
    "migration.title": "Équilibrage actif : migration en pause ({name})",
    "migration.message": (
        "L'ancien cycle d'équilibrage actif intégré n'a pas pu être migré "
        "en toute sécurité ({error}). La batterie est maintenue en mode manuel à 0 W. "
        "Gardez-la disponible et rechargez Omnibattery pour réessayer."
    ),
    # --- driver notices: control paused, firmware, settings, pacing ---
    "battery_control.paused_title": "Pilotage de la batterie en pause : {name}",
    "battery_control.realtime_mode_required": (
        "Omnibattery a mis en pause le pilotage de **{name}** car la batterie "
        "n'est pas en mode **Realtime**.\n\n"
        "Ouvrez l'application de la batterie et réglez le mode de fonctionnement sur "
        "Realtime. Omnibattery reprend alors automatiquement le pilotage."
    ),
    "battery_control.handover_failed": (
        "Omnibattery n'a pas pu prendre le pilotage de **{name}** : la batterie "
        "n'a pas confirmé la veille après une commande d'arrêt. Elle reste "
        "surveillée et Omnibattery réessaie automatiquement."
    ),
    "battery_control.unknown_model": (
        "**{name}** indique le code de modèle `{model}`, que Omnibattery ne prend "
        "pas encore en charge. La batterie est surveillée, mais pas pilotée. "
        "Merci de signaler ce code de modèle afin de pouvoir l'ajouter."
    ),
    "battery_control.firmware_title": "Mise à jour du firmware de la batterie recommandée : {name}",
    "battery_control.firmware": (
        "**{name}** utilise le firmware `{firmware}`. Le fabricant recommande au moins "
        "`{minimum}` pour l'API locale. La surveillance fonctionne ; mettez la "
        "batterie à jour dans son application si le pilotage se comporte "
        "mal."
    ),
    "battery_control.setting_title": "Réglage de la batterie non appliqué : {name}",
    "battery_control.setting_failed": (
        "Omnibattery n'a pas pu régler « {setting} » de **{name}** sur {value}. La "
        "batterie garde sa valeur actuelle, et Omnibattery affiche et utilise de "
        "nouveau cette valeur. Réessayez plus tard, ou modifiez-la dans "
        "l'application de la batterie."
    ),
    "battery_control.min_soc": "SOC minimal",
    "battery_control.max_charge_power": "puissance de charge maximale",
    "battery_control.max_discharge_power": "puissance de décharge maximale",
    "battery_control.throttled_title": "Batterie lente : {name}",
    "battery_control.throttled": (
        "**{name}** répond lentement ou pas du tout, donc Omnibattery attend "
        "désormais {interval:.0f}s entre les requêtes. Autre chose interroge peut-être "
        "aussi la batterie, comme une autre intégration Home Assistant, ou sa "
        "connexion réseau est faible."
    ),
    "battery_control.official": (
        "\n\nL'intégration officielle du fabricant interroge aussi cette batterie. "
        "Omnibattery est conçue pour la remplacer : désactivez cette intégration "
        "pour cet appareil afin de libérer le budget de requêtes de la "
        "batterie. Ses commandes (mode d'énergie, limite de décharge, limite "
        "d'injection) écrivent en outre dans la mémoire flash de la batterie à "
        "chaque utilisation."
    ),
    "battery_control.slower": (
        "\n\nSi cela se reproduit, abaissez d'un cran la vitesse des requêtes de "
        "**{name}**, sur **{speed}** : dans Paramètres → Appareils et services, "
        "choisissez Reconfigurer sur Omnibattery."
    ),
    "battery_control.speed_fast": "Rapide (2s)",
    "battery_control.speed_normal": "Normal (3s)",
    "battery_control.speed_conservative": "Conservateur (5s)",
}
