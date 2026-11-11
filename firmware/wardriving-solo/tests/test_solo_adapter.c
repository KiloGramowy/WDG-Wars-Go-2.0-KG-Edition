/* SPDX-License-Identifier: MIT
 * Execute the production adapter with fake SDK/tasks/radio and real cJSON.
 * This is deliberately NOT an ESP-IDF build or RF validation.
 */
#include <assert.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "solo_core.h"
#include "freertos/semphr.h"

typedef int esp_err_t;
typedef int wifi_band_mode_t;
typedef int wifi_second_chan_t;
typedef int wifi_band_t;
typedef int wifi_auth_mode_t;
typedef int wifi_init_config_t;
typedef struct { int unused; } wifi_ap_record_t;
typedef int portMUX_TYPE;
typedef void *TaskHandle_t;
typedef unsigned TickType_t;
typedef struct { unsigned filter_mask; } wifi_promiscuous_filter_t;
typedef struct { char cc[3]; uint8_t schan,nchan; int policy; uint32_t wifi_5g_channel_mask; } wifi_country_t;
typedef struct ble_gap_disc_params { unsigned itvl,window,filter_policy,limited,passive,filter_duplicates; } disc_params_t;
struct ble_gap_event { int unused; };
typedef struct { const char *command,*help,*hint; int (*func)(int,char **); void *argtable; } esp_console_cmd_t;
typedef struct { unsigned char bytes[128][256]; size_t size,count; } fake_queue_t;
typedef fake_queue_t *QueueHandle_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_STATE 2
#define IDLE 0
#define WIFI_BAND_MODE_AUTO 0
#define WIFI_SECOND_CHAN_NONE 0
#define WIFI_BAND_2G 0
#define WIFI_BAND_5G 1
#define WIFI_MODE_STA 1
#define WIFI_COUNTRY_POLICY_MANUAL 1
#define WIFI_PROMIS_FILTER_MASK_MGMT 1
#define BLE_HCI_SCAN_FILT_NO_WL 0
#define BLE_OWN_ADDR_PUBLIC 0
#define BLE_HS_FOREVER INT32_MAX
#define BLE_HS_EALREADY 2
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) ((void)(x))
#define portEXIT_CRITICAL(x) ((void)(x))
#define pdTRUE 1
#define pdPASS 1
#define pdMS_TO_TICKS(x) ((x)/10)  /* pinned CONFIG_FREERTOS_HZ=100 */
#define WIFI_INIT_CONFIG_DEFAULT() 0
#define JANOS_VERSION "1.7.0-test"
#define ESP_ERROR_CHECK(x) assert((x)==ESP_OK)

static bool wifi_initialized,wdg_solo_active,operation_stop_requested;
static bool event_loop_initialized,event_loop_exists,associated,arp_ban_active,pcap_arp_active;
static TaskHandle_t arp_ban_task_handle,pcap_arp_task_handle;
static int applicationState;
static unsigned loop_creates,loop_deletes;
static bool fail_wifi_init,fail_wifi_deinit,fail_loop_delete,fail_enable_promisc;
static uint64_t now_us,stop_at_us;
static unsigned opens,starts,stops,closes,tunes,task_creates,ble_starts,ble_cancels;
static bool driver_initialized,driver_started,promisc,busy,fail_send;
static int band_mode,current_channel;
static bool rejected[256],mismatched[256];
static disc_params_t last_ble;
static char emitted[256][4096];
static size_t emitted_count;
static bool inject_observation;
static uint8_t country_count=13;
static int country_policy;
static uint32_t country_mask;
static uint8_t ensure_country_count;
static bool fail_observation_write,fail_stopped_write;
static int ble_cancel_result;
static bool ble_driver_active;
static bool uart_locked;
static SemaphoreHandle_t xSemaphoreCreateMutex(void) { return (void *)(uintptr_t)1; }
static int xSemaphoreTake(SemaphoreHandle_t semaphore,unsigned timeout) {
    (void)semaphore; (void)timeout; assert(!uart_locked); uart_locked=true; return 1;
}
static int xSemaphoreGive(SemaphoreHandle_t semaphore) {
    (void)semaphore; assert(uart_locked); uart_locked=false; return 1;
}
static int test_printf(const char *format,...) {
    assert(uart_locked); /* Timestamp and output belong to one serialization lock. */
    assert(emitted_count<256);
    va_list args; va_start(args,format);
    int result=vsnprintf(emitted[emitted_count],4096,format,args);
    va_end(args);
    if ((fail_observation_write && strstr(emitted[emitted_count],"\"event\":\"observation\"")) ||
        (fail_stopped_write && strstr(emitted[emitted_count],"\"event\":\"stopped\""))) return -1;
    ++emitted_count; return result;
}
static int esp_event_loop_create_default(void) {
    ++loop_creates; if (event_loop_exists) return ESP_ERR_INVALID_STATE;
    event_loop_exists=true; return 0;
}
static int esp_event_loop_delete_default(void) {
    ++loop_deletes; assert(!driver_initialized && !driver_started);
    if (fail_loop_delete) return -1;
    assert(event_loop_exists); event_loop_exists=false; return 0;
}
static int esp_wifi_init(const wifi_init_config_t *config) {
    (void)config; ++opens; assert(event_loop_exists);
    if (fail_wifi_init) return -1;
    driver_initialized=true; return 0;
}
static int esp_wifi_set_mode(int mode) { (void)mode; return 0; }
static int esp_wifi_start(void) { ++starts; assert(event_loop_exists); driver_started=true; return 0; }
static int esp_wifi_stop(void) { ++stops; driver_started=false; return 0; }
static int esp_wifi_deinit(void) {
    ++closes; if (fail_wifi_deinit) return -1;
    driver_initialized=false; return 0;
}
static int esp_wifi_sta_get_ap_info(wifi_ap_record_t *record) { (void)record; return associated ? 0 : -1; }
static int esp_wifi_get_band_mode(wifi_band_mode_t *mode) { *mode=band_mode; return 0; }
static int esp_wifi_set_band_mode(wifi_band_mode_t mode) { band_mode=mode; return 0; }
static int esp_wifi_get_channel(uint8_t *ch,wifi_second_chan_t *second) {
    *ch=(uint8_t)(current_channel-(mismatched[current_channel] ? 1 : 0)); *second=0; return 0;
}
static int esp_wifi_set_channel(uint8_t channel,wifi_second_chan_t second) {
    (void)second; ++tunes;
    if (rejected[channel]) return -1;
    current_channel=channel; return 0;
}
static int esp_wifi_get_band(wifi_band_t *band) { *band=current_channel<=14 ? WIFI_BAND_2G : WIFI_BAND_5G; return 0; }
static int esp_wifi_get_promiscuous(bool *on) { *on=promisc; return 0; }
static int esp_wifi_set_promiscuous(bool on) {
    if (on && fail_enable_promisc) return -1;
    promisc=on; return 0;
}
static int esp_wifi_get_country(wifi_country_t *country) {
    *country=(wifi_country_t){{'P','H',0},1,country_count,country_policy,country_mask}; return 0;
}
static int esp_wifi_set_promiscuous_filter(const wifi_promiscuous_filter_t *filter) { (void)filter; return 0; }
static void wdp_promiscuous_cb(void *data,int type) { (void)data; (void)type; }
static int esp_wifi_set_promiscuous_rx_cb(void (*callback)(void *,int)) { (void)callback; return 0; }
static bool zig_recon_radio_busy(char *reason,size_t length) { (void)reason; (void)length; return busy; }
static bool zig_recon_is_active(void) { return false; }
static uint32_t esp_random(void) { return 0x12345678; }
static int64_t esp_timer_get_time(void) { return (int64_t)now_us; }
const char *esp_get_idf_version(void) { return "v6.0.1-test"; }
static bool ensure_wifi_mode(void) {
    wifi_initialized=true; driver_initialized=driver_started=true;
    if (ensure_country_count) country_count=ensure_country_count;
    return true;
}
static int bt_nimble_init(void) { return 0; }
static const char *get_auth_mode_wiggle(wifi_auth_mode_t mode) { (void)mode; return "WPA2"; }
static int bt_gap_event_callback(struct ble_gap_event *event,void *argument) { (void)event; (void)argument; return 0; }
static int ble_gap_disc(int address,int32_t duration,const struct ble_gap_disc_params *params,
                        int (*callback)(struct ble_gap_event *,void *),void *arg) {
    (void)address; (void)duration; (void)callback; (void)arg;
    ++ble_starts; last_ble=*params; ble_driver_active=true; return 0;
}
static int ble_gap_disc_cancel(void) {
    ++ble_cancels;
    if (!ble_cancel_result) ble_driver_active=false;
    return ble_cancel_result;
}
static int ble_gap_disc_active(void) { return ble_driver_active ? 1 : 0; }
static QueueHandle_t xQueueCreate(unsigned count,size_t size) {
    assert(count==128 && size<=256);
    QueueHandle_t q=calloc(1,sizeof(*q)); assert(q); q->size=size; return q;
}
static void xQueueReset(QueueHandle_t q) { q->count=0; }
static int xQueueSend(QueueHandle_t q,const void *row,unsigned ticks) {
    (void)ticks;
    if (fail_send || q->count>=128) return 0;
    memcpy(q->bytes[q->count++],row,q->size); return 1;
}
static int xQueueReceive(QueueHandle_t q,void *row,unsigned ticks) {
    (void)ticks;
    if (!q->count) return 0;
    memcpy(row,q->bytes[0],q->size);
    --q->count; memmove(q->bytes[0],q->bytes[1],q->count*256); return 1;
}
static unsigned uxQueueMessagesWaiting(QueueHandle_t q) { return (unsigned)q->count; }
static unsigned uxQueueSpacesAvailable(QueueHandle_t q) { return 128-(unsigned)q->count; }
static int xTaskCreate(void (*task)(void *),const char *name,unsigned stack,void *arg,
                       unsigned priority,TaskHandle_t *handle) {
    (void)task; (void)name; (void)stack; (void)arg; (void)priority;
    *handle=(void *)(uintptr_t)(++task_creates); return 1;
}
static void vTaskDelete(void *task) { (void)task; }
static void vTaskDelay(unsigned ticks);
static int esp_console_cmd_register(const esp_console_cmd_t *cmd) { assert(cmd->func); return 0; }
#define printf test_printf
#include "solo_adapter.inc"
#undef printf

static void vTaskDelay(unsigned ticks) {
    assert(ticks>0); /* A priority-5 zero-tick loop would starve the writer. */
    now_us+=(uint64_t)ticks*10000;
    if (inject_observation && solo_accepting && !solo_capturing_ble) {
        const uint8_t mac[]={0,1,2,3,4,5};
        wdg_solo_capture_wifi(mac,"fixture",7,0,(uint8_t)current_channel,-75);
        inject_observation=false;
    }
    if (stop_at_us && now_us>=stop_at_us) solo_stop_requested=true;
    /* Cooperatively execute only the real writer's finite drain after RF stop.
     * This is a deterministic task fake, not concurrency/hardware evidence. */
    if (solo_writer_stop && solo_writer_handle) solo_writer(NULL);
}
static cJSON *event_at(size_t n) {
    assert(n<emitted_count && !strncmp(emitted[n],"@SOLO ",6));
    cJSON *root=cJSON_Parse(emitted[n]+6); assert(root); return root;
}
static bool event_is(size_t n,const char *event) {
    cJSON *root=event_at(n);
    const cJSON *item=cJSON_GetObjectItem(root,"event");
    bool okay=cJSON_IsString(item) && !strcmp(item->valuestring,event);
    cJSON_Delete(root); return okay;
}
static void reset(void) {
    free(solo_queue); solo_queue=NULL;
    wifi_initialized=driver_initialized=driver_started=true;
    wdg_solo_active=solo_stop_requested=solo_accepting=solo_writer_stop=false;
    solo_ble_running=false; solo_task_handle=solo_writer_handle=NULL;
    solo_callbacks_inflight=0; solo_seq=solo_dropped=0; solo_phase_mac_count=0;
    solo_session[0]=0; solo_boot_id[0]=0; emitted_count=0;
    solo_phase_next_evict=0; solo_stopped_frame_written=solo_writer_control_failed=false;
    solo_uart_mutex=xSemaphoreCreateMutex(); uart_locked=false;
    now_us=stop_at_us=0; band_mode=0; current_channel=6;
    opens=starts=stops=closes=tunes=task_creates=ble_starts=ble_cancels=0;
    promisc=busy=fail_send=inject_observation=false;
    country_count=13; country_policy=0; country_mask=0;
    ensure_country_count=0; fail_observation_write=fail_stopped_write=false;
    ble_cancel_result=0; ble_driver_active=false;
    event_loop_initialized=event_loop_exists=true;
    associated=arp_ban_active=pcap_arp_active=false;
    arp_ban_task_handle=pcap_arp_task_handle=NULL; applicationState=IDLE;
    loop_creates=loop_deletes=0;
    fail_wifi_init=fail_wifi_deinit=fail_loop_delete=fail_enable_promisc=false;
    solo_probe_driver_owned=solo_probe_loop_owned=solo_probe_unresolved=false;
    memset(rejected,0,sizeof(rejected)); memset(mismatched,0,sizeof(mismatched));
}
static int start(const char *profile,const char *wifi,const char *ble) {
    char *args[]={"wardrive_solo","0123456789abcdef",(char *)profile,(char *)wifi,(char *)ble};
    return cmd_wardrive_solo(5,args);
}
static void caps_readback_and_restore(void) {
    reset(); promisc=true; rejected[177]=true; mismatched[173]=true;
    assert(solo_probe_channels(true)); assert(current_channel==6 && promisc && band_mode==0);
    assert(!wdg_solo_active && !solo_queue && event_is(0,"caps"));
    cJSON *root=event_at(0);
    assert(cJSON_GetArraySize(cJSON_GetObjectItem(root,"channels"))==39);
    assert(cJSON_GetArraySize(cJSON_GetObjectItem(root,"skipped"))==3);
    assert(!cJSON_GetObjectItem(root,"lat")); cJSON_Delete(root);
}
static void temporary_caps_restore_rf_off(void) {
    reset(); wifi_initialized=driver_initialized=driver_started=false;
    assert(solo_probe_channels(true));
    assert(opens==1 && starts==1 && stops==1 && closes==1);
    assert(!wifi_initialized && !driver_initialized && !driver_started && !wdg_solo_active);
}
static void cold_caps_owns_only_its_temporary_event_loop(void) {
    reset(); wifi_initialized=driver_initialized=driver_started=false;
    event_loop_initialized=event_loop_exists=false;
    assert(solo_probe_channels(true));
    assert(loop_creates==1 && loop_deletes==1 && !event_loop_exists && !event_loop_initialized);
    reset(); wifi_initialized=driver_initialized=driver_started=false;
    event_loop_initialized=false; /* Existing loop, not owned by JanOS' flag. */
    assert(solo_probe_channels(true));
    assert(loop_creates==1 && loop_deletes==0 && event_loop_exists && !event_loop_initialized);
    reset(); wifi_initialized=driver_initialized=driver_started=false;
    event_loop_initialized=event_loop_exists=false; fail_wifi_init=true;
    assert(!solo_probe_channels(true));
    assert(loop_creates==1 && loop_deletes==1 && !event_loop_exists && !wdg_solo_active);
}
static void probe_cleanup_failures_require_verified_stop(void) {
    reset(); wifi_initialized=driver_initialized=driver_started=false;
    event_loop_initialized=event_loop_exists=false; fail_wifi_deinit=true;
    assert(!solo_probe_channels(true));
    assert(wdg_solo_active && solo_probe_unresolved && solo_probe_driver_owned && solo_probe_loop_owned);
    cJSON *error=event_at(0);
    assert(!strcmp(cJSON_GetObjectItem(error,"code")->valuestring,"restore_failed"));
    assert(!cJSON_GetObjectItem(error,"session")); cJSON_Delete(error);
    assert(start("full","1","1")==1 && !task_creates);
    assert(event_loop_exists && !loop_deletes && !wdg_solo_stop());
    fail_wifi_deinit=false;
    assert(wdg_solo_stop() && !wdg_solo_active && !solo_probe_unresolved && !event_loop_exists);
    reset(); wifi_initialized=driver_initialized=driver_started=false;
    event_loop_initialized=event_loop_exists=false; fail_loop_delete=true;
    assert(!solo_probe_channels(true)); assert(!solo_probe_driver_owned && solo_probe_loop_owned);
    assert(!wdg_solo_stop()); fail_loop_delete=false;
    assert(wdg_solo_stop() && !wdg_solo_active && !event_loop_exists);
    reset(); promisc=true; fail_enable_promisc=true;
    assert(!solo_probe_channels(true) && solo_probe_unresolved && wdg_solo_active);
    assert(wdg_solo_stop() && !promisc && !wdg_solo_active && event_loop_initialized);
}
static void associated_and_arp_owners_reject_without_tuning(void) {
    for (int reason=0;reason<6;++reason) {
        reset();
        if (reason==0) associated=true;
        else if (reason==1) arp_ban_active=true;
        else if (reason==2) arp_ban_task_handle=(void *)1;
        else if (reason==3) pcap_arp_active=true;
        else if (reason==4) pcap_arp_task_handle=(void *)1;
        else applicationState=1;
        assert(cmd_wardrive_solo_caps(1,NULL)==1 && start("full","1","1")==1);
        assert(!tunes && !opens && !task_creates && !wdg_solo_active);
    }
}
static void channel14_and_country_mask_policy_are_runtime_checked(void) {
    reset(); country_count=14;
    assert(solo_probe_channels(true)); assert(solo_accepted_count==42);
    assert(solo_accepted[13]==14);
    reset(); country_count=14; rejected[14]=true;
    assert(solo_probe_channels(true)); assert(solo_accepted_count==41);
    for (size_t i=0;i<solo_accepted_count;++i) assert(solo_accepted[i]!=14);
    reset(); country_mask=2;  /* AUTO ignores stale/manual-only bitmask. */
    assert(solo_probe_channels(true)); assert(solo_accepted_count==41);
    reset(); country_policy=WIFI_COUNTRY_POLICY_MANUAL; country_mask=2;
    assert(solo_probe_channels(true)); assert(solo_accepted_count==14);
}
static void busy_caps_never_tune(void) {
    reset(); busy=true;
    assert(cmd_wardrive_solo_caps(1,NULL)==1);
    assert(tunes==0 && opens==0 && !wdg_solo_active && event_is(0,"error"));
}
static void kg_never_silently_shortens(void) {
    reset(); rejected[48]=true;
    assert(start("kg","1","1")==1);
    assert(!wdg_solo_active && !solo_queue && task_creates==0);
}
static void start_ack_waits_for_actual_phase_and_stop_drains(void) {
    reset(); assert(start("kg","1","0")==0);
    assert(emitted_count==1 && event_is(0,"caps") && wdg_solo_active && task_creates==2);
    stop_at_us=20000; inject_observation=true;
    solo_task(NULL);
    assert(!wdg_solo_active && !promisc && solo_writer_handle==NULL);
    assert(emitted_count==4 && event_is(1,"state") &&
           event_is(2,"observation") && event_is(3,"stopped"));
}
static void actual_start_caps_supersede_idle_channel_set(void) {
    reset(); assert(solo_probe_channels(true)); assert(solo_accepted_count==41);
    ensure_country_count=14;
    assert(start("full","1","0")==0);
    assert(emitted_count==2 && event_is(0,"caps") && event_is(1,"caps"));
    cJSON *root=event_at(1);
    assert(cJSON_GetArraySize(cJSON_GetObjectItem(root,"channels"))==42);
    cJSON_Delete(root);
    stop_at_us=20000; solo_task(NULL);
    assert(event_is(2,"state") && !wdg_solo_active);
}
static void ble_solo_preserves_native_active_coex_parameters(void) {
    reset(); assert(start("full","0","1")==0);
    stop_at_us=20000; solo_task(NULL);
    assert(ble_starts==1 && ble_cancels==1);
    assert(last_ble.passive==0 && last_ble.itvl==0x100 && last_ble.window==0x40);
    assert(!wdg_solo_active && event_is(emitted_count-1,"stopped"));
}
static void full_ble_is_passive_one_second_and_not_parallel_wifi(void) {
    reset(); assert(start("full","1","1")==0);
    stop_at_us=14855000; solo_task(NULL);
    assert(ble_starts==1 && last_ble.passive==1);
    assert(last_ble.itvl==160 && last_ble.window==160);
    assert(!wdg_solo_active && !promisc && event_is(emitted_count-1,"stopped"));
}
static void raw_capture_time_hex_and_dedup(void) {
    reset(); strcpy(solo_session,"0123456789abcdef");
    solo_queue=xQueueCreate(128,sizeof(solo_message_t));
    wdg_solo_active=solo_accepting=true; solo_capturing_ble=false;
    const uint8_t mac[]={0,1,2,3,4,5}; const char name[]={ (char)0xff,0,'A' };
    now_us=100;
    wdg_solo_capture_wifi(mac,name,3,0,6,-80);
    wdg_solo_capture_wifi(mac,name,3,0,6,-90);
    assert(solo_seq==1 && solo_queue->count==1);
    now_us=5000; solo_writer_stop=true; solo_writer(NULL);
    cJSON *root=event_at(0);
    assert(cJSON_GetObjectItem(root,"capture_monotonic_us")->valuedouble==100);
    assert(cJSON_GetObjectItem(root,"emitted_monotonic_us")->valuedouble==5000);
    assert(!strcmp(cJSON_GetObjectItem(root,"name_bytes_hex")->valuestring,"ff0041"));
    assert(!strcmp(cJSON_GetObjectItem(root,"ssid")->valuestring,"??A"));
    assert(!cJSON_GetObjectItem(root,"gps") && !cJSON_GetObjectItem(root,"lat"));
    cJSON_Delete(root);
    const uint8_t other[]={6,1,2,3,4,5}; fail_send=true;
    wdg_solo_capture_wifi(other,"x",1,0,6,-70); assert(solo_dropped==1);
}
static void continuous_ble_ring_and_name_enrichment_keep_new_observations(void) {
    reset(); strcpy(solo_session,"0123456789abcdef");
    solo_queue=xQueueCreate(128,sizeof(solo_message_t));
    wdg_solo_active=solo_accepting=solo_capturing_ble=true;
    for (unsigned i=0;i<300;++i) {
        uint8_t mac[]={1,2,3,4,(uint8_t)(i>>8),(uint8_t)i};
        solo_capture(true,mac,NULL,0,NULL,0,-90);
        solo_message_t message;
        assert(xQueueReceive(solo_queue,&message,0)==pdTRUE);
        assert(message.value.observation.seq==i+1);
    }
    assert(solo_seq==300 && solo_dropped==0 && solo_phase_mac_count==256);
    uint8_t mac[]={1,2,3,4,1,43};
    solo_capture(true,mac,(const uint8_t *)"named",5,NULL,0,-90);
    solo_message_t message; assert(xQueueReceive(solo_queue,&message,0)==pdTRUE);
    assert(message.value.observation.seq==301 && message.value.observation.name_length==5);
    solo_capture(true,mac,(const uint8_t *)"named",5,NULL,0,-90);
    assert(!solo_queue->count);
    solo_capture(true,mac,(const uint8_t *)"named",5,NULL,0,-80);
    assert(xQueueReceive(solo_queue,&message,0)==pdTRUE);
    assert(message.value.observation.seq==302 && message.value.observation.rssi==-80);
}
static void stopped_drop_count_includes_writer_failures(void) {
    reset(); assert(start("kg","1","0")==0);
    stop_at_us=20000; inject_observation=true; fail_observation_write=true;
    solo_task(NULL);
    assert(!wdg_solo_active && solo_dropped==1);
    cJSON *root=event_at(emitted_count-1);
    assert(!strcmp(cJSON_GetObjectItem(root,"event")->valuestring,"stopped"));
    assert(cJSON_GetObjectItem(root,"dropped")->valuedouble==1);
    cJSON_Delete(root);
}
static void failed_completion_retains_owner_until_explicit_recovery(void) {
    reset(); assert(start("kg","1","0")==0);
    stop_at_us=20000; fail_stopped_write=true; solo_task(NULL);
    assert(wdg_solo_active && !solo_stopped_frame_written && solo_writer_control_failed);
    assert(!promisc && !solo_task_handle && !solo_writer_handle);
    assert(!wdg_solo_stop() && wdg_solo_active);
    for (size_t i=0;i<emitted_count;++i) assert(!event_is(i,"stopped"));
    fail_stopped_write=false;
    assert(wdg_solo_stop() && !wdg_solo_active && event_is(emitted_count-1,"stopped"));
}
static void ble_cancel_failure_keeps_truthful_owner_on_retries(void) {
    reset(); assert(start("full","0","1")==0);
    stop_at_us=20000; ble_cancel_result=99; solo_task(NULL);
    assert(wdg_solo_active && solo_ble_running && ble_driver_active);
    assert(!solo_task_handle && !solo_writer_handle);
    for (size_t i=0;i<emitted_count;++i) assert(!event_is(i,"stopped"));
    assert(!wdg_solo_stop()); assert(solo_ble_running && ble_driver_active && wdg_solo_active);
    unsigned previous=ble_cancels;
    assert(!wdg_solo_stop()); assert(ble_cancels>previous && wdg_solo_active);
    ble_cancel_result=0;
    assert(wdg_solo_stop() && !wdg_solo_active && !ble_driver_active);
}
static void unresolved_stop_never_fakes_completion(void) {
    reset(); wdg_solo_active=true; solo_task_handle=(void *)1;
    assert(!wdg_solo_stop()); assert(wdg_solo_active && event_is(0,"error"));
    assert(emitted_count==1);
}
static void reused_session_rejected_and_registration_is_additive(void) {
    reset(); strcpy(solo_session,"0123456789abcdef");
    assert(start("full","1","0")==1 && !wdg_solo_active && !task_creates);
    wdg_solo_register();
    const uint8_t reversed[]={6,5,4,3,2,1};
    solo_queue=xQueueCreate(128,sizeof(solo_message_t));
    wdg_solo_active=solo_accepting=solo_capturing_ble=true;
    wdg_solo_capture_ble(reversed,(const uint8_t *)"ble",3,-70);
    assert(solo_queue->count==1);
    solo_message_t message; assert(xQueueReceive(solo_queue,&message,0)==pdTRUE);
    solo_observation_t row=message.value.observation;
    assert(row.mac[0]==1 && row.mac[5]==6 && row.ble);
}
int main(void) {
    caps_readback_and_restore(); temporary_caps_restore_rf_off(); busy_caps_never_tune();
    cold_caps_owns_only_its_temporary_event_loop(); probe_cleanup_failures_require_verified_stop();
    associated_and_arp_owners_reject_without_tuning();
    channel14_and_country_mask_policy_are_runtime_checked();
    kg_never_silently_shortens(); start_ack_waits_for_actual_phase_and_stop_drains();
    actual_start_caps_supersede_idle_channel_set();
    ble_solo_preserves_native_active_coex_parameters();
    full_ble_is_passive_one_second_and_not_parallel_wifi(); raw_capture_time_hex_and_dedup();
    continuous_ble_ring_and_name_enrichment_keep_new_observations();
    stopped_drop_count_includes_writer_failures();
    failed_completion_retains_owner_until_explicit_recovery();
    ble_cancel_failure_keeps_truthful_owner_on_retries();
    unresolved_stop_never_fakes_completion(); reused_session_rejected_and_registration_is_additive();
    reset(); free(solo_queue);
    puts("19 native adapter tests passed with fake SDK/tasks and real cJSON; no RF/IDF build.");
    return 0;
}
