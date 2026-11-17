#Laboratorio 17/11/2026 - serra intelligente: DHT22, fotoresistore, potenziometro, servo, stepper, OLED, 2 pulsanti, 2 LED, buzzer
#ricordarsi di salvare ssd1306.py sulla scheda (Thonny -> Salva su dispositivo MicroPython)

from machine import Pin, ADC, PWM, I2C
import dht
import ssd1306
import time

#collegamenti: il mio circuito (pin suggeriti dalla traccia accanto, nel commento)
PIN_LDR = 33                      # traccia: 34
PIN_POT = 34                      # traccia: 35
PIN_DHT = 4                       # traccia: 4  (DATA del DHT22, VCC a 3.3V)
PIN_SERVO = 18                    # traccia: 18
PIN_STEPPER = [19, 21, 22, 23]    # traccia: 16, 17, 19, 23 (IN1-IN4 del ULN2003)
SCL_PIN = 17                      # traccia: 22 (21 e 22 nel mio circuito sono dello stepper)
SDA_PIN = 16                      # traccia: 21
PIN_PULSANTE1 = 27                # traccia: 32 (modalita' AUTO/MANUALE)
PIN_PULSANTE2 = 14                # traccia: 33 (comando multifunzione)
PIN_LED_VERDE = 25                # traccia: 25
PIN_LED_ROSSO = 32                # traccia: 26 (e' il LED che ho gia' sul 32)
PIN_BUZZER = 26                   # traccia: 27

#modalita'
AUTO = "AUTO"
MANUALE = "MANUALE"

#parametri della traccia
SOGLIA_MIN = 22                   # la soglia di temperatura si sceglie col potenziometro tra 22...
SOGLIA_MAX = 32                   # ...e 32 gradi
ISTERESI_TEMP = 2                 # la finestra si richiude 2 gradi sotto la soglia
ALLARME_TEMP = 35                 # sopra 35 gradi -> allarme temperatura
ALLARME_UMID = 35                 # sotto il 35% -> allarme umidita'

#tempi in ms
PERIODO_DHT = 2000                # il DHT22 non va interrogato piu' spesso di ogni 2 s
PERIODO_ANALOGICI = 100           # fotoresistore e potenziometro ogni 100 ms
PERIODO_DISPLAY = 500             # display aggiornato ogni mezzo secondo
CONFERMA_LUCE = 1000              # la nuova fascia di luce deve durare 1 s prima di muovere la tenda
TEMPO_SILENZIO = 30000            # il buzzer resta zitto per 30 s dopo il pulsante 2

#tenda: il 28BYJ-48 in half-step fa 4096 passi per giro (2048 in full-step)
#assumo che la corsa completa della tenda sia mezzo giro del rullo: 4096 / 2 = 2048 half-step
CORSA_TENDA = 2048
NOMI_TENDA = ["APERTA", "INTERMEDIA", "CHIUSA"]
PASSI_TENDA = [0, CORSA_TENDA // 2, CORSA_TENDA]   # 0 = aperta (posizione di partenza), 1024 = a meta', 2048 = chiusa

#servo: duty a 10 bit con PWM a 50 Hz
DUTY_MIN = 26                     # 26/1024*20 ms = circa 0.5 ms -> 0 gradi
DUTY_MAX = 123                    # 123/1024*20 ms = circa 2.4 ms -> 180 gradi


class LDR:
    """Legge il fotoresistore e converte il valore ADC nell'intervallo [min, max]"""

    def __init__(self, pin, min_value=0, max_value=100):
        """
        :parameter pin pin collegato al fotoresistore
        :parameter min_value valore minimo restituito da value()
        :parameter max_value valore massimo restituito da value()
        """
        if min_value >= max_value:
            raise Exception('Min value is greater or equal to max value')
        self.adc = ADC(Pin(pin))
        self.adc.atten(ADC.ATTN_11DB)   # lettura su tutto 0-3.3V
        self.min_value = min_value
        self.max_value = max_value

    def read(self):
        """
        :return valore grezzo da 0 a 4095 (media di 8 letture per togliere un po' di rumore)
        """
        somma = 0
        for i in range(8):
            somma += self.adc.read()
        return somma // 8

    def value(self):
        """
        :return valore proporzionale nell'intervallo [min, max]
        """
        return self.min_value + (self.max_value - self.min_value) * self.read() / 4095


class POTENZIOMETRO:
    """Potenziometro tra 3.3V e GND, il cursore sull'ADC"""

    def __init__(self, pin):
        self.adc = ADC(Pin(pin))
        self.adc.atten(ADC.ATTN_11DB)   # 0-3.3V -> 0-4095

    def read(self):
        somma = 0
        for i in range(8):
            somma += self.adc.read()
        return somma // 8

    def mappa(self, minimo, massimo):
        # porta la lettura 0-4095 nell'intervallo [minimo, massimo]
        return minimo + (massimo - minimo) * self.read() / 4095


class LED:
    """LED spento, acceso o lampeggiante senza bloccare il programma"""

    def __init__(self, pin):
        self.pin = Pin(pin, Pin.OUT)
        self.pin.off()
        self.modo = "spento"
        self.t_on = 0
        self.t_off = 0
        self.t_inizio = time.ticks_ms()

    def accendi(self):
        self.modo = "acceso"
        self.pin.on()

    def spegni(self):
        self.modo = "spento"
        self.pin.off()

    def lampeggia(self, t_on, t_off):
        self.modo = "lampeggio"
        self.t_on = t_on
        self.t_off = t_off
        self.t_inizio = time.ticks_ms()

    def aggiorna(self):
        if self.modo != "lampeggio":
            return
        # guardo in che punto del periodo mi trovo: prima parte acceso, seconda spento
        fase = time.ticks_diff(time.ticks_ms(), self.t_inizio) % (self.t_on + self.t_off)
        self.pin.value(1 if fase < self.t_on else 0)


class BUZZER:
    """Buzzer passivo in PWM: suona una sequenza di (frequenza, durata) senza bloccare"""

    def __init__(self, sig_pin):
        self.pwm = PWM(Pin(sig_pin, Pin.OUT), freq=1000)
        self.pwm.duty(0)        # inizialmente spento
        self.sequenza = []
        self.indice = 0
        self.ripeti = False
        self.t_inizio = time.ticks_ms()

    def _nota(self, freq):
        if freq == 0:
            self.pwm.duty(0)    # pausa
        else:
            self.pwm.freq(freq)
            self.pwm.duty(512)  # circa 50%

    def suona(self, sequenza, ripeti=False):
        self.sequenza = sequenza
        self.indice = 0
        self.ripeti = ripeti
        self.t_inizio = time.ticks_ms()
        self._nota(sequenza[0][0])

    def ferma(self):
        self.sequenza = []
        self.pwm.duty(0)

    def occupato(self):
        return len(self.sequenza) > 0

    def aggiorna(self):
        if not self.sequenza:
            return
        ora = time.ticks_ms()
        freq, durata = self.sequenza[self.indice]
        if time.ticks_diff(ora, self.t_inizio) >= durata:
            # passo alla nota successiva
            self.indice += 1
            self.t_inizio = ora
            if self.indice >= len(self.sequenza):
                if self.ripeti:
                    self.indice = 0
                else:
                    self.ferma()
                    return
            self._nota(self.sequenza[self.indice][0])


class STEPPER:
    """28BYJ-48 con ULN2003 in half-step: fa al massimo un passo per chiamata di aggiorna()"""

    # half-step: alterna 1 bobina e 2 bobine
    SEQUENZA_HALF = [
        [1, 0, 0, 0],
        [1, 0, 0, 1],
        [0, 0, 0, 1],
        [0, 0, 1, 1],
        [0, 0, 1, 0],
        [0, 1, 1, 0],
        [0, 1, 0, 0],
        [1, 1, 0, 0],
    ]
    INTERVALLO = 3      # almeno 3 ms tra due passi

    def __init__(self, pins):
        self.pins = [Pin(p, Pin.OUT) for p in pins]
        self.indice = 0          # riga corrente della sequenza
        self.posizione = 0       # posizione in half-step: 0 = tenda aperta (messa a mano all'avvio)
        self.obiettivo = 0
        self.t_ultimo = time.ticks_ms()
        self._spegni_bobine()

    def _spegni_bobine(self):
        for p in self.pins:
            p.value(0)

    def vai_a(self, posizione):
        self.obiettivo = posizione

    def arrivato(self):
        return self.posizione == self.obiettivo

    def aggiorna(self):
        if self.posizione == self.obiettivo:
            return
        ora = time.ticks_ms()
        if time.ticks_diff(ora, self.t_ultimo) < self.INTERVALLO:
            return
        if self.obiettivo > self.posizione:
            # chiudo la tenda: verso orario (direzione -1 = orario)
            self.indice = (self.indice - 1) % len(self.SEQUENZA_HALF)
            self.posizione += 1
        else:
            # apro la tenda: verso antiorario
            self.indice = (self.indice + 1) % len(self.SEQUENZA_HALF)
            self.posizione -= 1
        for i in range(4):
            self.pins[i].value(self.SEQUENZA_HALF[self.indice][i])
        self.t_ultimo = ora
        if self.posizione == self.obiettivo:
            self._spegni_bobine()    # arrivato: tolgo corrente, la posizione resta in memoria


class PULSANTE:
    """Pulsante tra pin e GND con pull-up interno: premuto = 0, interrupt sul fronte di discesa"""

    DEBOUNCE = 200      # ms, ignoro i fronti piu' vicini di 200 ms
    RILASCIO = 50       # il pin deve restare alto 50 ms per dire che il tasto e' stato lasciato

    def __init__(self, pin):
        self.pin = Pin(pin, Pin.IN, Pin.PULL_UP)
        self.premuto = False     # pressione da gestire nel loop
        self.giu = False         # tasto ancora tenuto premuto
        self.ultimo = 0
        self.t_alto = 0
        self.pin.irq(trigger=Pin.IRQ_FALLING, handler=self._interrupt)

    def _interrupt(self, pin):
        # handler corto: segna solo la pressione
        t = time.ticks_ms()
        if self.giu or time.ticks_diff(t, self.ultimo) < self.DEBOUNCE:
            return               # rimbalzo o tasto gia' giu': ignoro
        self.ultimo = t
        self.giu = True
        self.premuto = True

    def evento(self):
        # True una sola volta per ogni pressione
        ora = time.ticks_ms()
        if self.giu:
            if self.pin.value() == 0:
                self.t_alto = ora            # ancora premuto
            elif time.ticks_diff(ora, self.t_alto) >= self.RILASCIO:
                self.giu = False             # rilasciato davvero
                self.ultimo = ora            # i rimbalzi del rilascio vengono ignorati
        if self.premuto:
            self.premuto = False
            return True
        return False


#inizializzazione dei componenti
sensor = dht.DHT22(Pin(PIN_DHT))
ldr = LDR(PIN_LDR, min_value=0, max_value=100)
pot = POTENZIOMETRO(PIN_POT)
servo_pwm = PWM(Pin(PIN_SERVO), freq=50)          # 50 Hz = periodo 20 ms
tenda = STEPPER(PIN_STEPPER)
i2c = I2C(0, scl=Pin(SCL_PIN), sda=Pin(SDA_PIN))  # bus I2C numero 0
display = ssd1306.SSD1306_I2C(128, 64, i2c)
pulsante1 = PULSANTE(PIN_PULSANTE1)
pulsante2 = PULSANTE(PIN_PULSANTE2)
led_verde = LED(PIN_LED_VERDE)
led_rosso = LED(PIN_LED_ROSSO)
buzzer = BUZZER(PIN_BUZZER)

#stato del sistema
modalita = AUTO
temperatura = None        # None finche' il DHT22 non ha dato la prima lettura
umidita = None
luce = 0                  # luminosita' 0-100%
soglia = SOGLIA_MIN       # soglia di temperatura scelta col potenziometro
angolo_servo = None       # ultimo angolo comandato (None = nessun comando ancora)
finestra_aperta = False   # stato della ventilazione in AUTO (serve per l'isteresi)
tenda_obiettivo = 0       # indice in NOMI_TENDA della posizione richiesta
fascia_luce = 0           # fascia di luce confermata (0, 1, 2)
fascia_candidata = 0      # fascia letta adesso, in attesa di conferma
t_fascia = 0
cause = []                # cause di allarme attive
silenziato = False
t_silenzio = 0

ora_avvio = time.ticks_ms()
t_dht = ora_avvio         # la prima lettura del DHT arriva dopo 2 s (gli serve tempo all'accensione)
t_analogici = ora_avvio
t_display = ora_avvio


def imposta_angolo(angolo):
    # porta il servo all'angolo 0-180 col duty tra 26 e 123, e lo ricorda
    global angolo_servo
    angolo = max(0, min(180, angolo))   # evito angoli fuori range
    duty = DUTY_MIN + (DUTY_MAX - DUTY_MIN) * angolo / 180
    servo_pwm.duty(int(round(duty)))
    angolo_servo = angolo


def muovi_tenda(indice):
    # porta la tenda in una delle tre posizioni, solo se non e' gia' quella richiesta
    global tenda_obiettivo
    if indice == tenda_obiettivo:
        return                           # gia' li' (o ci sta andando): nessun movimento
    passi = PASSI_TENDA[indice] - tenda.posizione   # > 0 chiude (orario), < 0 apre (antiorario)
    print("Tenda: {} -> {} ({} half-step)".format(NOMI_TENDA[tenda_obiettivo], NOMI_TENDA[indice], passi))
    tenda_obiettivo = indice
    tenda.vai_a(PASSI_TENDA[indice])


def fascia_da_luce(percento):
    # 0-30% luce insufficiente, 31-70% normale, 71-100% intensa
    if percento <= 30:
        return 0          # tenda aperta
    elif percento <= 70:
        return 1          # tenda a meta'
    else:
        return 2          # tenda chiusa


def leggi_dht():
    # una misura ogni 2 s; se la lettura fallisce tengo i valori di prima
    global temperatura, umidita
    try:
        sensor.measure()
        temperatura = sensor.temperature()
        umidita = sensor.humidity()
    except OSError:
        print("Errore lettura DHT22, tengo i valori precedenti")


def leggi_sensori():
    global t_dht, t_analogici, luce, soglia
    ora = time.ticks_ms()
    if time.ticks_diff(ora, t_dht) >= PERIODO_DHT:
        t_dht = ora
        leggi_dht()
        print("T: {} C, U: {} %, luce: {} %, soglia: {:.1f} C, modo: {}, cause: {}".format(
            temperatura, umidita, luce, soglia, modalita, cause))
    if time.ticks_diff(ora, t_analogici) >= PERIODO_ANALOGICI:
        t_analogici = ora
        luce = int(round(ldr.value()))       # 0-100%
        soglia = pot.mappa(SOGLIA_MIN, SOGLIA_MAX)   # 22-32 gradi (in MANUALE non viene usata)


def controlla_ventilazione():
    global finestra_aperta
    if modalita == MANUALE:
        # il potenziometro comanda direttamente la finestra tra 0 e 90 gradi
        angolo = int(pot.mappa(0, 90))
        if angolo_servo is None or abs(angolo - angolo_servo) >= 2:   # ignoro il tremolio di 1 grado
            imposta_angolo(angolo)
        return
    if temperatura is None:
        return                               # ancora nessuna temperatura valida
    # isteresi: apro sopra la soglia, richiudo solo 2 gradi sotto
    if temperatura > soglia:
        finestra_aperta = True
    elif temperatura <= soglia - ISTERESI_TEMP:
        finestra_aperta = False
    angolo = 90 if finestra_aperta else 0
    if angolo != angolo_servo:               # comando solo se cambia
        imposta_angolo(angolo)
        print("Finestra", "aperta" if finestra_aperta else "chiusa")


def controlla_schermatura():
    global fascia_luce, fascia_candidata, t_fascia
    nuova = fascia_da_luce(luce)
    ora = time.ticks_ms()
    if nuova != fascia_candidata:
        fascia_candidata = nuova
        t_fascia = ora                        # la fascia e' cambiata: riparte il conteggio
    elif nuova != fascia_luce and time.ticks_diff(ora, t_fascia) >= CONFERMA_LUCE:
        fascia_luce = nuova                   # stabile da 1 s: la confermo
    if modalita == AUTO:
        muovi_tenda(fascia_luce)              # non fa nulla se la tenda e' gia' li'
    tenda.aggiorna()                          # un passo al massimo


def sequenza_allarme():
    # suono diverso per capire la causa anche senza guardare il display
    if len(cause) == 2:
        return [(2000, 300), (0, 150), (1000, 300), (0, 300)]
    if cause[0] == "TEMP":
        return [(2000, 300), (0, 300)]
    return [(1000, 300), (0, 300)]


def gestisci_allarmi():
    global cause, silenziato
    nuove = []
    if temperatura is not None and temperatura > ALLARME_TEMP:
        nuove.append("TEMP")
    if umidita is not None and umidita < ALLARME_UMID:
        nuove.append("UMID")

    if nuove != cause:
        if not nuove:
            # tutto rientrato: rosso spento, verde acceso, buzzer zitto
            led_rosso.spegni()
            led_verde.accendi()
            silenziato = False
            print("Funzionamento normale")
        else:
            if not cause:
                led_verde.spegni()
                led_rosso.lampeggia(250, 250)    # 2 Hz
            for c in nuove:
                if c not in cause:
                    silenziato = False           # una causa nuova fa ripartire il buzzer
            print("ALLARME:", nuove)
        buzzer.ferma()                           # la sequenza giusta riparte qui sotto
        cause = nuove

    if silenziato and time.ticks_diff(time.ticks_ms(), t_silenzio) >= TEMPO_SILENZIO:
        silenziato = False                       # finito il silenzio temporaneo

    if cause and not silenziato and not buzzer.occupato():
        buzzer.suona(sequenza_allarme(), ripeti=True)

    led_rosso.aggiorna()
    buzzer.aggiorna()


def gestisci_pulsanti():
    global modalita, silenziato, t_silenzio, angolo_servo
    if pulsante1.evento():
        if modalita == AUTO:
            modalita = MANUALE
        else:
            modalita = AUTO
            angolo_servo = None      # forzo il ricalcolo: la finestra torna dove dice la temperatura
        print("Modalita':", modalita)

    if pulsante2.evento():
        if modalita == MANUALE:
            # aperta -> intermedia -> chiusa -> aperta
            muovi_tenda((tenda_obiettivo + 1) % 3)
        elif cause:
            silenziato = True
            t_silenzio = time.ticks_ms()
            buzzer.ferma()           # il LED rosso continua a lampeggiare
            print("Buzzer silenziato per", TEMPO_SILENZIO // 1000, "s")
        else:
            print("Nessun allarme da silenziare")


def aggiorna_display():
    global t_display
    ora = time.ticks_ms()
    if time.ticks_diff(ora, t_display) < PERIODO_DISPLAY:
        return
    t_display = ora
    display.fill(0)                                   # schermo tutto nero
    display.text("SERRA-G39", 0, 0)
    display.text(modalita, 128 - 8 * len(modalita), 0)   # modalita' allineata a destra (8 pixel per carattere)
    if temperatura is None:
        display.text("T: --  U: --", 0, 12)
    else:
        display.text("T:{:.1f}C U:{:.0f}%".format(temperatura, umidita), 0, 12)
    if modalita == AUTO:
        display.text("L:{}% Sog:{:.1f}".format(luce, soglia), 0, 22)
    else:
        display.text("L:{}% man.".format(luce), 0, 22)
    display.text("Finestra:{}".format(angolo_servo if angolo_servo is not None else 0), 0, 32)
    if tenda.arrivato():
        display.text("Tenda:" + NOMI_TENDA[tenda_obiettivo], 0, 42)
    else:
        display.text("Tenda>" + NOMI_TENDA[tenda_obiettivo], 0, 42)    # ">" = in movimento verso...
    if cause:
        if silenziato:
            resto = (TEMPO_SILENZIO - time.ticks_diff(ora, t_silenzio)) // 1000
            display.text("ALL. muto {}s".format(resto), 0, 54)
        else:
            display.text("ALL:" + " ".join(c for c in cause), 0, 54)   # es. "ALL:TEMP UMID"
    else:
        display.text("Stato: normale", 0, 54)
    display.show()                                    # senza show() non compare niente


#avvio: finestra chiusa, tenda aperta (messa a mano), LED verde acceso
imposta_angolo(0)
led_verde.accendi()
print("Serra avviata, prima lettura del DHT22 tra 2 s")

while True:
    leggi_sensori()
    gestisci_pulsanti()
    controlla_ventilazione()
    controlla_schermatura()
    gestisci_allarmi()
    aggiorna_display()
    time.sleep_ms(1)      # pausa minima, il loop resta reattivo
