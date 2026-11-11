/* SPDX-License-Identifier: MIT
 * Included by the pinned main.c after its BLE helpers. Legacy paths are gated
 * only while this opt-in owner is active. No GPS assignment or SD I/O here.
 */
#include "solo_core.h"
#include "esp_idf_version.h"
#include "freertos/semphr.h"

#define SOLO_QUEUE_LENGTH 128
#define SOLO_PHASE_IDENTITIES 256
typedef struct {
    uint64_t seq, capture_us;
    bool ble;
    uint8_t mac[6], channel;
    int8_t rssi;
    uint8_t name_bytes[32], name_length;
    char auth[40];
} solo_observation_t;
typedef struct {
    bool control;
    union { solo_observation_t observation; cJSON *event; } value;
} solo_message_t;
static portMUX_TYPE solo_mux = portMUX_INITIALIZER_UNLOCKED;
static QueueHandle_t solo_queue;
static TaskHandle_t solo_task_handle;
static TaskHandle_t solo_writer_handle;
static SemaphoreHandle_t solo_uart_mutex;
static volatile bool solo_stop_requested, solo_accepting;
static volatile bool solo_writer_stop;
static volatile bool solo_stopped_frame_written, solo_writer_control_failed;
static bool solo_capturing_ble;
static unsigned solo_callbacks_inflight;
static uint64_t solo_seq, solo_dropped;
static uint8_t solo_phase_macs[SOLO_PHASE_IDENTITIES][6];
static uint8_t solo_phase_names[SOLO_PHASE_IDENTITIES][32];
static uint8_t solo_phase_name_lengths[SOLO_PHASE_IDENTITIES];
static int8_t solo_phase_best_rssi[SOLO_PHASE_IDENTITIES];
static size_t solo_phase_mac_count,solo_phase_next_evict;
static char solo_boot_id[17], solo_session[17];
static solo_plan_t solo_plan;
static uint8_t solo_accepted[SOLO_MAX_CHANNELS];
static size_t solo_accepted_count;
static bool solo_capabilities_valid;
static bool solo_ble_running;
static bool solo_probe_driver_owned,solo_probe_loop_owned,solo_probe_unresolved;

/* The pinned SDK uses a 100 Hz tick: 5 ms truncates to zero ticks. A zero
 * delay only yields to equal-priority tasks and would starve the UART writer. */
static void solo_delay_ms(unsigned milliseconds) {
    TickType_t ticks=pdMS_TO_TICKS(milliseconds);
    vTaskDelay(ticks ? ticks : 1);
}

static void solo_boot_init(void) {
    if (!solo_boot_id[0])
        snprintf(solo_boot_id,sizeof(solo_boot_id),"%08lx%08lx",
                 (unsigned long)esp_random(),(unsigned long)esp_random());
}
static cJSON *solo_event(const char *event, bool session) {
    solo_boot_init();
    cJSON *root=cJSON_CreateObject();
    if (!root) return NULL;
    cJSON_AddNumberToObject(root,"v",1);
    cJSON_AddStringToObject(root,"event",event);
    cJSON_AddStringToObject(root,"boot_id",solo_boot_id);
    cJSON_AddNumberToObject(root,"emitted_monotonic_us",(double)esp_timer_get_time());
    if (session && solo_session[0]) cJSON_AddStringToObject(root,"session",solo_session);
    return root;
}
static bool solo_write(cJSON *root) {
    if (!root) return false;
    if (!solo_uart_mutex || xSemaphoreTake(solo_uart_mutex,portMAX_DELAY)!=pdTRUE) {
        cJSON_Delete(root); return false;
    }
    cJSON *emitted=cJSON_GetObjectItem(root,"emitted_monotonic_us");
    if (!emitted) {
        cJSON_Delete(root); xSemaphoreGive(solo_uart_mutex); return false;
    }
    double clock=(double)esp_timer_get_time();
    cJSON_SetNumberValue(emitted,clock);
    cJSON *anchor=cJSON_GetObjectItem(root,"clock_monotonic_us");
    if (anchor) cJSON_SetNumberValue(anchor,clock);
    char *text=cJSON_PrintUnformatted(root);
    bool okay=text && strlen(text)+6<=4096;
    if (okay) {
        int written=printf("@SOLO %s\n",text);
        okay=written==(int)(strlen(text)+7) && fflush(stdout)==0;
    }
    free(text); cJSON_Delete(root);
    xSemaphoreGive(solo_uart_mutex);
    return okay;
}
static bool solo_emit(cJSON *root) {
    if (!root) return false;
    if (!solo_writer_handle) return solo_write(root);  /* idle console/caps */
    solo_message_t message={.control=true,.value.event=root};
    if (xQueueSend(solo_queue,&message,0)==pdTRUE) return true;
    cJSON_Delete(root);
    solo_stop_requested=true;  /* No success ACK for an unreportable phase. */
    return false;
}
static void solo_drop(void) {
    portENTER_CRITICAL(&solo_mux); ++solo_dropped; portEXIT_CRITICAL(&solo_mux);
}
static void solo_error(const char *code, const char *message) {
    cJSON *root=solo_event("error",wdg_solo_active && !solo_probe_unresolved);
    if (!root) return;
    cJSON_AddStringToObject(root,"code",code);
    cJSON_AddStringToObject(root,"message",message);
    solo_emit(root);
}
static bool solo_other_owner(void) {
    char reason[40];
    wifi_ap_record_t association;
    return applicationState!=IDLE || arp_ban_active || arp_ban_task_handle ||
        pcap_arp_active || pcap_arp_task_handle ||
        (wifi_initialized && esp_wifi_sta_get_ap_info(&association)==ESP_OK) ||
        zig_recon_radio_busy(reason,sizeof(reason)) || zig_recon_is_active();
}
static uint32_t solo_5g_bit(uint8_t channel) {
    static const uint8_t order[]={36,40,44,48,52,56,60,64,100,104,108,112,
        116,120,124,128,132,136,140,144,149,153,157,161,165,169,173,177};
    for (size_t i=0;i<sizeof(order);++i) if (order[i]==channel) return UINT32_C(1)<<(i+1);
    return 0;
}
/* Resource ownership survives a failed probe cleanup. In particular, never
 * delete an event loop while the WiFi driver which posts to it is still alive. */
static bool solo_probe_cleanup(void) {
    if (solo_probe_driver_owned) {
        esp_wifi_stop();
        if (esp_wifi_deinit()!=ESP_OK) return false;
        solo_probe_driver_owned=false;
    }
    if (solo_probe_loop_owned) {
        if (esp_event_loop_delete_default()!=ESP_OK) return false;
        solo_probe_loop_owned=false;
    }
    return true;
}
/* Caps may temporarily initialize a bare STA driver, without netifs or changing
 * JanOS' mode bookkeeping/NVS country. Every exit restores the previous RF. */
static bool solo_probe_channels(bool emit) {
    bool temporary=!wifi_initialized, snapshot=false;
    bool previous_promisc=false, okay=false, restored=true;
    wifi_band_mode_t previous_mode=WIFI_BAND_MODE_AUTO;
    uint8_t previous_channel=1;
    wifi_second_chan_t previous_second=WIFI_SECOND_CHAN_NONE;
    wifi_country_t country={0};
    cJSON *root=NULL, *accepted=NULL, *skipped=NULL;
    solo_capabilities_valid=false; solo_accepted_count=0;
    if (temporary) {
        if (!event_loop_initialized) {
            esp_err_t loop=esp_event_loop_create_default();
            if (loop==ESP_OK) solo_probe_loop_owned=true;
            else if (loop!=ESP_ERR_INVALID_STATE) goto done;
        }
        wifi_init_config_t init=WIFI_INIT_CONFIG_DEFAULT();
        if (esp_wifi_init(&init)!=ESP_OK) goto done;
        solo_probe_driver_owned=true;
        if (esp_wifi_set_mode(WIFI_MODE_STA)!=ESP_OK || esp_wifi_start()!=ESP_OK) goto done;
    }
    if (esp_wifi_get_band_mode(&previous_mode)!=ESP_OK ||
        esp_wifi_get_channel(&previous_channel,&previous_second)!=ESP_OK ||
        esp_wifi_get_promiscuous(&previous_promisc)!=ESP_OK ||
        esp_wifi_get_country(&country)!=ESP_OK) goto done;
    snapshot=true;
    if (esp_wifi_set_promiscuous(false)!=ESP_OK ||
        esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO)!=ESP_OK) goto done;
    if (emit) {
        root=solo_event("caps",false);
        if (!root) goto done;
        accepted=cJSON_AddArrayToObject(root,"channels");
        skipped=cJSON_AddArrayToObject(root,"skipped");
        if (!accepted || !skipped) goto done;
    }
    for (size_t i=0;i<SOLO_MAX_CHANNELS;++i) {
        uint8_t channel=solo_full_candidates[i], actual=0;
        wifi_second_chan_t second;
        wifi_band_t band;
        const char *reason=NULL;
        if (channel<=14 && (channel<country.schan ||
            channel>=(unsigned)country.schan+country.nchan)) reason="country_range";
        else if (channel>14 && country.policy==WIFI_COUNTRY_POLICY_MANUAL &&
                 country.wifi_5g_channel_mask &&
                 !(country.wifi_5g_channel_mask&solo_5g_bit(channel))) reason="country_mask";
        else if (esp_wifi_set_channel(channel,WIFI_SECOND_CHAN_NONE)!=ESP_OK) reason="driver_rejected";
        else if (esp_wifi_get_channel(&actual,&second)!=ESP_OK || actual!=channel ||
                 esp_wifi_get_band(&band)!=ESP_OK ||
                 band!=(channel<=14 ? WIFI_BAND_2G : WIFI_BAND_5G)) reason="readback_mismatch";
        if (!reason) {
            solo_accepted[solo_accepted_count++]=channel;
            if (accepted) cJSON_AddItemToArray(accepted,cJSON_CreateNumber(channel));
        } else if (skipped) {
            cJSON *item=cJSON_CreateObject();
            cJSON_AddNumberToObject(item,"channel",channel);
            cJSON_AddStringToObject(item,"reason",reason);
            cJSON_AddItemToArray(skipped,item);
        }
    }
    okay=solo_accepted_count>0;
done:
    if (snapshot) {
        if (esp_wifi_set_promiscuous(false)!=ESP_OK) restored=false;
        if (esp_wifi_set_band_mode(previous_mode)!=ESP_OK) restored=false;
        if (esp_wifi_set_channel(previous_channel,previous_second)!=ESP_OK) restored=false;
        if (esp_wifi_set_promiscuous(previous_promisc)!=ESP_OK) restored=false;
    }
    if (temporary && !solo_probe_cleanup()) restored=false;
    okay=okay && restored;
    solo_capabilities_valid=okay;
    if (okay && root) {
        cJSON_AddBoolToObject(root,"supported",true);
        cJSON_AddNumberToObject(root,"contract",1);
        cJSON_AddStringToObject(root,"firmware",JANOS_VERSION);
        cJSON_AddStringToObject(root,"sdk",esp_get_idf_version());
        cJSON_AddNumberToObject(root,"max_line_bytes",4096);
        cJSON_AddNumberToObject(root,"clock_monotonic_us",(double)esp_timer_get_time());
        cJSON *info=cJSON_AddObjectToObject(root,"country");
        char cc[4]={country.cc[0],country.cc[1],country.cc[2],0};
        cJSON_AddStringToObject(info,"code",cc);
        cJSON_AddNumberToObject(info,"policy",country.policy);
        cJSON_AddNumberToObject(info,"first",country.schan);
        cJSON_AddNumberToObject(info,"count",country.nchan);
        cJSON_AddNumberToObject(info,"mask5",country.wifi_5g_channel_mask);
        return solo_emit(root);
    }
    cJSON_Delete(root);
    if (!restored) {
        solo_probe_unresolved=true;
        wdg_solo_active=true; /* Existing legacy mode gates remain fail-closed. */
    }
    if (!okay) solo_error(restored ? "capability_failed" : "restore_failed",
                         "Cannot verify and restore radio channels");
    return okay;
}
static int cmd_wardrive_solo_caps(int argc,char **argv) {
    (void)argv;
    if (argc!=1) { solo_error("arguments","Use wardrive_solo_caps"); return 1; }
    if (wdg_solo_active || solo_writer_handle || solo_task_handle || solo_other_owner()) {
        solo_error("busy","Stop the active radio operation first"); return 1;
    }
    return solo_probe_channels(true) ? 0 : 1;
}
static void solo_capture(bool ble,const uint8_t *mac,const uint8_t *name,size_t length,
                         const char *auth,uint8_t channel,int8_t rssi) {
    solo_observation_t row={0};
    portENTER_CRITICAL(&solo_mux);
    if (!wdg_solo_active || !solo_accepting ||
        ble!=solo_capturing_ble) { portEXIT_CRITICAL(&solo_mux); return; }
    portEXIT_CRITICAL(&solo_mux);
    /* Reserve room for state/error/stopped frames; radio callbacks never wait
     * for UART or allocation and do not fill the control reserve. */
    if (uxQueueSpacesAvailable(solo_queue)<=8) { solo_drop(); return; }
    portENTER_CRITICAL(&solo_mux);
    if (!wdg_solo_active || !solo_accepting ||
        ble!=solo_capturing_ble) { portEXIT_CRITICAL(&solo_mux); return; }
    size_t bounded_length=name ? (length>32 ? 32 : length) : 0;
    size_t slot=solo_phase_mac_count;
    for (size_t i=0;i<solo_phase_mac_count;++i) {
        if (!memcmp(solo_phase_macs[i],mac,6)) {
            bool changed_name=bounded_length &&
                (bounded_length!=solo_phase_name_lengths[i] ||
                 memcmp(solo_phase_names[i],name,bounded_length));
            if (!changed_name && rssi<=solo_phase_best_rssi[i]) {
                portEXIT_CRITICAL(&solo_mux); return;
            }
            slot=i; break;
        }
    }
    bool existing=slot<solo_phase_mac_count;
    if (!existing) {
        if (solo_phase_mac_count<SOLO_PHASE_IDENTITIES) ++solo_phase_mac_count;
        else {
            slot=solo_phase_next_evict;
            solo_phase_next_evict=(solo_phase_next_evict+1)%SOLO_PHASE_IDENTITIES;
        }
        memcpy(solo_phase_macs[slot],mac,6);
        solo_phase_name_lengths[slot]=0;
        solo_phase_best_rssi[slot]=rssi;
    }
    if (bounded_length) {
        memcpy(solo_phase_names[slot],name,bounded_length);
        solo_phase_name_lengths[slot]=(uint8_t)bounded_length;
    }
    if (rssi>solo_phase_best_rssi[slot]) solo_phase_best_rssi[slot]=rssi;
    row.seq=++solo_seq; row.capture_us=(uint64_t)esp_timer_get_time();
    ++solo_callbacks_inflight;
    portEXIT_CRITICAL(&solo_mux);
    row.ble=ble; memcpy(row.mac,mac,6); row.channel=channel; row.rssi=rssi;
    row.name_length=(uint8_t)bounded_length;
    if (row.name_length) memcpy(row.name_bytes,name,row.name_length);
    if (auth) snprintf(row.auth,sizeof(row.auth),"%s",auth);
    solo_message_t message={.control=false,.value.observation=row};
    if (xQueueSend(solo_queue,&message,0)!=pdTRUE) solo_drop();
    portENTER_CRITICAL(&solo_mux); --solo_callbacks_inflight; portEXIT_CRITICAL(&solo_mux);
}
static void wdg_solo_capture_wifi(const uint8_t *mac,const char *ssid,size_t length,
                                wifi_auth_mode_t auth,uint8_t channel,int8_t rssi) {
    solo_capture(false,mac,(const uint8_t *)ssid,length,get_auth_mode_wiggle(auth),channel,rssi);
}
static void wdg_solo_capture_ble(const uint8_t *mac,const uint8_t *name,size_t length,int8_t rssi) {
    /* NimBLE stores address octets reversed relative to canonical display. */
    uint8_t canonical[6];
    for (int i=0;i<6;++i) canonical[i]=mac[5-i];
    solo_capture(true,canonical,name,length,NULL,0,rssi);
}
static void solo_output_row(const solo_observation_t *row) {
    cJSON *root=solo_event("observation",true);
    if (!root) { solo_drop(); return; }
    char mac[18];
    snprintf(mac,sizeof(mac),"%02X:%02X:%02X:%02X:%02X:%02X",
        row->mac[0],row->mac[1],row->mac[2],row->mac[3],row->mac[4],row->mac[5]);
    cJSON_AddNumberToObject(root,"seq",(double)row->seq);
    cJSON_AddNumberToObject(root,"capture_monotonic_us",(double)row->capture_us);
    cJSON_AddStringToObject(root,"protocol",row->ble ? "ble" : "wifi");
    cJSON_AddStringToObject(root,"mac",mac);
    char display[33],raw_hex[65];
    solo_display_name(display,row->name_bytes,row->name_length);
    for (size_t i=0;i<row->name_length;++i)
        snprintf(raw_hex+2*i,3,"%02x",row->name_bytes[i]);
    raw_hex[row->name_length*2]=0;
    cJSON_AddStringToObject(root,row->ble ? "name" : "ssid",display);
    cJSON_AddStringToObject(root,"name_bytes_hex",raw_hex);
    if (!row->ble) {
        cJSON_AddStringToObject(root,"auth",row->auth);
        cJSON_AddNumberToObject(root,"channel",row->channel);
    }
    cJSON_AddNumberToObject(root,"rssi",row->rssi);
    if (!solo_write(root)) solo_drop();
}
static void solo_writer(void *argument) {
    (void)argument;
    solo_message_t message;
    while (!solo_writer_stop || uxQueueMessagesWaiting(solo_queue)) {
        if (xQueueReceive(solo_queue,&message,pdMS_TO_TICKS(20))==pdTRUE) {
            if (message.control) {
                cJSON *kind=cJSON_GetObjectItem(message.value.event,"event");
                bool stopped=cJSON_IsString(kind) && !strcmp(kind->valuestring,"stopped");
                if (stopped) {
                    portENTER_CRITICAL(&solo_mux);
                    uint64_t dropped=solo_dropped;
                    portEXIT_CRITICAL(&solo_mux);
                    cJSON *field=cJSON_GetObjectItem(message.value.event,"dropped");
                    if (field) cJSON_SetNumberValue(field,(double)dropped);
                }
                if (!solo_write(message.value.event)) {
                    solo_writer_control_failed=true; solo_stop_requested=true;
                } else if (stopped) solo_stopped_frame_written=true;
            } else solo_output_row(&message.value.observation);
        }
    }
    solo_writer_handle=NULL;
    vTaskDelete(NULL);
}
static bool solo_drain_writer(void) {
    solo_writer_stop=true;
    for (int i=0;i<350 && solo_writer_handle;++i) solo_delay_ms(10);
    return solo_writer_handle==NULL;
}
static bool solo_state(void) {
    cJSON *root=solo_event("state",true);
    if (!root) return false;
    cJSON_AddNumberToObject(root,"clock_monotonic_us",(double)esp_timer_get_time());
    cJSON_AddStringToObject(root,"profile",solo_plan.profile==SOLO_KG ? "kg" : "full");
    cJSON_AddBoolToObject(root,"wifi",solo_plan.wifi);
    cJSON_AddBoolToObject(root,"ble",solo_plan.ble);
    cJSON_AddStringToObject(root,"phase",solo_plan.phase==SOLO_WIFI ? "wifi" :
                            solo_plan.phase==SOLO_BLE ? "ble" : "pause");
    cJSON_AddNumberToObject(root,"cycles",(double)solo_plan.cycles);
    return solo_emit(root);
}
static bool solo_quiet(void) {
    portENTER_CRITICAL(&solo_mux); solo_accepting=false; portEXIT_CRITICAL(&solo_mux);
    bool okay=esp_wifi_set_promiscuous(false)==ESP_OK;
    bool receiving=true;
    if (esp_wifi_get_promiscuous(&receiving)!=ESP_OK || receiving) okay=false;
    if (solo_ble_running) {
        int cancel=ble_gap_disc_cancel();
        bool active=ble_gap_disc_active()!=0;
        if ((cancel!=0 && cancel!=BLE_HS_EALREADY) || active) okay=false;
        else solo_ble_running=false;
    }
    for (int i=0;i<100;++i) {
        portENTER_CRITICAL(&solo_mux);
        bool quiet=solo_callbacks_inflight==0;
        portEXIT_CRITICAL(&solo_mux);
        if (quiet) return okay;
        vTaskDelay(1);
    }
    return false;
}
static bool solo_apply_phase(bool fresh_phase,solo_phase_t previous_phase) {
    if (!solo_quiet()) return false;
    if (fresh_phase && previous_phase==SOLO_WIFI && solo_plan.cycles) {
        solo_plan.wifi_complete_us=(uint64_t)esp_timer_get_time();
        if (solo_plan.phase==SOLO_WAIT)
            solo_plan.deadline_us=solo_plan.wifi_complete_us+SOLO_KG_GAP_US;
    }
    if (fresh_phase) {
        portENTER_CRITICAL(&solo_mux);
        solo_phase_mac_count=solo_phase_next_evict=0;
        portEXIT_CRITICAL(&solo_mux);
    }
    if (solo_plan.phase==SOLO_WAIT) return solo_state();
    if (solo_plan.phase==SOLO_WIFI) {
        uint8_t requested=solo_plan.channels[solo_plan.index].channel,actual;
        wifi_second_chan_t second;
        if (esp_wifi_set_channel(requested,WIFI_SECOND_CHAN_NONE)!=ESP_OK ||
            esp_wifi_get_channel(&actual,&second)!=ESP_OK || actual!=requested) return false;
        bool receiving=false;
        if (esp_wifi_set_promiscuous(true)!=ESP_OK ||
            esp_wifi_get_promiscuous(&receiving)!=ESP_OK || !receiving) {
            portENTER_CRITICAL(&solo_mux); solo_accepting=false; portEXIT_CRITICAL(&solo_mux);
            return false;
        }
        solo_plan.deadline_us=(uint64_t)esp_timer_get_time()+
            (uint64_t)solo_plan.channels[solo_plan.index].dwell_ms*1000;
    } else {
        struct ble_gap_disc_params params={
            .itvl=0x100,.window=0x40,.filter_policy=BLE_HCI_SCAN_FILT_NO_WL,
            .limited=0,.passive=solo_plan.wifi ? 1 : 0,.filter_duplicates=0};
        /* JanOS native SOLO uses 160/40 active; combined bursts use full-duty
         * passive listening, never parallel WiFi. Piglet interval/window=100ms. */
        if (solo_plan.wifi) params.itvl=params.window=160;
        if (ble_gap_disc(BLE_OWN_ADDR_PUBLIC,BLE_HS_FOREVER,&params,
                         bt_gap_event_callback,NULL)!=0) {
            portENTER_CRITICAL(&solo_mux); solo_accepting=false; portEXIT_CRITICAL(&solo_mux);
            return false;
        }
        solo_ble_running=true;
        if (!ble_gap_disc_active()) return false;
        if (solo_plan.wifi)
            solo_plan.deadline_us=(uint64_t)esp_timer_get_time()+SOLO_BLE_BURST_US;
    }
    if (fresh_phase && !solo_state()) return false;
    /* Confirmed phase is queued ahead of its first observation. All session
     * frames share one writer, so slow stdout cannot block the RF scheduler. */
    portENTER_CRITICAL(&solo_mux);
    solo_capturing_ble=solo_plan.phase==SOLO_BLE; solo_accepting=true;
    portEXIT_CRITICAL(&solo_mux);
    return true;
}
static void solo_task(void *argument) {
    (void)argument;
    bool okay=true;
    solo_plan_start(&solo_plan,(uint64_t)esp_timer_get_time());
    if (!solo_apply_phase(true,SOLO_OFF)) { solo_error("radio_start","Radio phase did not start"); okay=false; }
    while (okay && !solo_stop_requested) {
        solo_phase_t before=solo_plan.phase;
        if (solo_plan_tick(&solo_plan,(uint64_t)esp_timer_get_time())) {
            bool fresh=before!=solo_plan.phase ||
                       (solo_plan.phase==SOLO_WIFI && solo_plan.index==0);
            if (!solo_apply_phase(fresh,before)) { solo_error("radio_phase","Radio transition failed"); okay=false; }
        }
        solo_delay_ms(5);
    }
    if (!solo_quiet()) {
        solo_error("stop_failed","Radio callbacks did not become idle");
        /* Stop accepting was already set. Drain existing rows without a false
         * completion so an explicit STOP can retry failed driver cancellation. */
        solo_drain_writer();
        /* Keep ownership unresolved; cmd_stop must not send a success ACK. */
        solo_task_handle=NULL; vTaskDelete(NULL); return;
    }
    solo_plan_stop(&solo_plan);
    cJSON *root=solo_event("stopped",true);
    if (root) {
        cJSON_AddNumberToObject(root,"last_seq",(double)solo_seq);
        cJSON_AddNumberToObject(root,"dropped",0); /* Writer finalizes after FIFO drain. */
        if (!solo_emit(root)) {
            solo_drain_writer();
            solo_task_handle=NULL; vTaskDelete(NULL); return;
        }
    }
    else { solo_drain_writer(); solo_task_handle=NULL; vTaskDelete(NULL); return; }
    if (!solo_drain_writer()) {
        solo_error("drain_timeout","UART drain did not finish; radio ownership retained");
        solo_task_handle=NULL; vTaskDelete(NULL); return;
    }
    if (!solo_stopped_frame_written || solo_writer_control_failed) {
        solo_error("completion_failed","SOLO completion was not written; radio ownership retained");
        solo_task_handle=NULL; vTaskDelete(NULL); return;
    }
    wdg_solo_active=false;
    solo_task_handle=NULL;
    vTaskDelete(NULL);
}
static bool wdg_solo_stop(void) {
    if (solo_probe_unresolved) {
        bool quiet=solo_probe_cleanup();
        if (quiet && wifi_initialized) {
            bool receiving=true;
            quiet=esp_wifi_set_promiscuous(false)==ESP_OK &&
                esp_wifi_get_promiscuous(&receiving)==ESP_OK && !receiving;
        }
        if (!quiet) { solo_error("restore_failed","Probe resources are not idle; retry STOP"); return false; }
        solo_probe_unresolved=false; wdg_solo_active=false;
        return true; /* No session was accepted; only the universal STOP ACK. */
    }
    if (!wdg_solo_active) return true;
    /* A separate, explicit retry may recover a previously failed completion,
     * but never reinterpret the first failed ACK as success in the same call. */
    if (!solo_task_handle && !solo_writer_handle && solo_queue &&
        !uxQueueMessagesWaiting(solo_queue) && solo_quiet()) {
        cJSON *root=solo_event("stopped",true);
        if (!root) return false;
        cJSON_AddNumberToObject(root,"last_seq",(double)solo_seq);
        cJSON_AddNumberToObject(root,"dropped",(double)solo_dropped);
        if (!solo_write(root)) return false;
        solo_stopped_frame_written=true; solo_writer_control_failed=false;
        wdg_solo_active=false; return true;
    }
    solo_stop_requested=true;
    for (int i=0;i<400 && wdg_solo_active;++i) solo_delay_ms(10);
    if (wdg_solo_active) { solo_error("stop_timeout","SOLO did not stop; radio ownership retained"); return false; }
    return true;
}
static int cmd_wardrive_solo(int argc,char **argv) {
    if (argc!=5 || !solo_session_valid(argv[1]) ||
        (strcmp(argv[2],"full") && strcmp(argv[2],"kg")) ||
        (strcmp(argv[3],"0") && strcmp(argv[3],"1")) ||
        (strcmp(argv[4],"0") && strcmp(argv[4],"1"))) {
        solo_error("arguments","Use wardrive_solo session16 full|kg wifi0|1 ble0|1"); return 1;
    }
    if (wdg_solo_active || solo_writer_handle || solo_task_handle || solo_other_owner()) {
        solo_error("busy","Stop the active radio operation first"); return 1;
    }
    if (!solo_uart_mutex) return 1;
    if (solo_session[0] && !strcmp(solo_session,argv[1])) {
        solo_error("session_reused","Choose a new session identifier after STOP"); return 1;
    }
    bool wifi=argv[3][0]=='1',ble=argv[4][0]=='1';
    if (!wifi && !ble) { solo_error("arguments","Enable WiFi or BLE"); return 1; }
    if (!ensure_wifi_mode() || !solo_probe_channels(true) ||
        !solo_plan_init(&solo_plan,!strcmp(argv[2],"kg") ? SOLO_KG : SOLO_FULL,
                         wifi,ble,solo_accepted,solo_accepted_count)) {
        solo_error("profile_unavailable","Cannot initialize the complete requested profile"); return 1;
    }
    if (ble && bt_nimble_init()!=ESP_OK) { solo_error("ble_init","Cannot initialize BLE"); return 1; }
    if (!solo_queue) solo_queue=xQueueCreate(SOLO_QUEUE_LENGTH,sizeof(solo_message_t));
    if (!solo_queue) { solo_error("memory","Cannot allocate the observation queue"); return 1; }
    xQueueReset(solo_queue);
    strcpy(solo_session,argv[1]); solo_seq=solo_dropped=0;
    solo_phase_mac_count=solo_phase_next_evict=0;
    solo_callbacks_inflight=0; solo_stop_requested=false;
    solo_stopped_frame_written=solo_writer_control_failed=false;
    wifi_promiscuous_filter_t filter={.filter_mask=WIFI_PROMIS_FILTER_MASK_MGMT};
    if (esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO)!=ESP_OK ||
        esp_wifi_set_promiscuous_filter(&filter)!=ESP_OK ||
        esp_wifi_set_promiscuous_rx_cb(wdp_promiscuous_cb)!=ESP_OK) {
        solo_error("wifi_init","Cannot configure radio capture"); return 1;
    }
    operation_stop_requested=false; solo_writer_stop=false; wdg_solo_active=true;
    if (xTaskCreate(solo_writer,"wdg_solo_uart",4096,NULL,4,&solo_writer_handle)!=pdPASS) {
        wdg_solo_active=false; solo_writer_handle=NULL;
        solo_error("task_init","Cannot start the UART writer"); return 1;
    }
    if (xTaskCreate(solo_task,"wdg_solo",8192,NULL,5,&solo_task_handle)!=pdPASS) {
        solo_drain_writer();
        wdg_solo_active=false; solo_task_handle=NULL;
        solo_error("task_init","Cannot start the SOLO task"); return 1;
    }
    return 0;
}
static void wdg_solo_register(void) {
    solo_boot_init();
    if (!solo_uart_mutex) solo_uart_mutex=xSemaphoreCreateMutex();
    const esp_console_cmd_t caps={.command="wardrive_solo_caps",
        .help="Report validated Wardriving SOLO capabilities",.hint=NULL,
        .func=cmd_wardrive_solo_caps,.argtable=NULL};
    const esp_console_cmd_t start={.command="wardrive_solo",
        .help="Start raw Wardriving SOLO: session16 full|kg wifi0|1 ble0|1",
        .hint=NULL,.func=cmd_wardrive_solo,.argtable=NULL};
    ESP_ERROR_CHECK(esp_console_cmd_register(&caps));
    ESP_ERROR_CHECK(esp_console_cmd_register(&start));
}
