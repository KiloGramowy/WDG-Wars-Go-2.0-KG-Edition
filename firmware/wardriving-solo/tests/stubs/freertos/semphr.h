/* Native-test SDK boundary only. */
#ifndef SOLO_TEST_SEMPHR_H
#define SOLO_TEST_SEMPHR_H
typedef void *SemaphoreHandle_t;
#define portMAX_DELAY UINT32_MAX
static SemaphoreHandle_t xSemaphoreCreateMutex(void);
static int xSemaphoreTake(SemaphoreHandle_t,unsigned);
static int xSemaphoreGive(SemaphoreHandle_t);
#endif
