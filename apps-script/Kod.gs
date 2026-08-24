/**
 * Portföy tablosu — fon fiyatlarını doğrudan hücrelere yazar.
 *
 * IMPORTDATA kullanılmıyor. Gelen değer JSON sayısı olduğu ve hücreye
 * setValue ile sayı olarak yazıldığı için ne CSV ayrıştırması, ne ondalık
 * ayırıcı, ne de yan sütuna taşma sorunu oluşuyor.
 *
 * Kurulum:
 *   1. Uzantılar > Apps Script, bu dosyayı yapıştır, kaydet.
 *   2. Bir kez `fiyatlariGuncelle` çalıştır (izin ister, onayla).
 *   3. Saat simgesi (Tetikleyiciler) > Tetikleyici ekle:
 *      fiyatlariGuncelle / Zamana dayalı / Günlük / 11:00-12:00
 *      (servis 10:30'da topluyor, 11:00 güvenli.)
 */

var SERVIS = 'https://fon.niladsp.com.tr';
var SAYFA = 'veri';       // fon kodlarının bulunduğu sayfa
var KOD_SATIRI = 2;       // fon kodları bu satırda
var FIYAT_SATIRI = 4;     // fiyat buraya yazılacak
var GETIRI_SATIRI = 5;    // günlük getiri buraya yazılacak (% olarak)

function fiyatlariGuncelle() {
  var sayfa = SpreadsheetApp.getActive().getSheetByName(SAYFA);
  if (!sayfa) throw new Error('Sayfa bulunamadi: ' + SAYFA);

  var sonSutun = sayfa.getLastColumn();
  var kodlar = sayfa.getRange(KOD_SATIRI, 1, 1, sonSutun).getValues()[0];

  // Boş olmayan hücreleri aday kod say. Altın satırları ("22 AYAR", "ATA",
  // "ONS") servis tarafından bulunamayacağı için kendiliğinden elenir ve
  // o sütunlara dokunulmaz.
  var adaylar = [];
  for (var i = 0; i < kodlar.length; i++) {
    var kod = String(kodlar[i]).trim().toUpperCase();
    if (kod) adaylar.push({ kod: kod, sutun: i + 1 });
  }
  if (!adaylar.length) return;

  var url = SERVIS + '/coklu?fonlar=' + encodeURIComponent(
    adaylar.map(function (a) { return a.kod; }).join(',')
  );

  var cevap = UrlFetchApp.fetch(url, {
    muteHttpExceptions: true,
    followRedirects: true
  });
  if (cevap.getResponseCode() !== 200) {
    throw new Error('Servis hatasi ' + cevap.getResponseCode() + ': ' +
                    cevap.getContentText().slice(0, 200));
  }

  var veri = JSON.parse(cevap.getContentText());
  var yazilan = 0;

  adaylar.forEach(function (aday) {
    var f = veri.fonlar[aday.kod];
    if (!f || !f.bulundu) return;  // fon değil (altın vb.) ya da kod yanlış

    // setValue sayı yazar; hücre biçimlendirmesi görünümü belirler.
    sayfa.getRange(FIYAT_SATIRI, aday.sutun).setValue(f.fiyat);
    if (GETIRI_SATIRI && f.gunluk_getiri !== null) {
      sayfa.getRange(GETIRI_SATIRI, aday.sutun).setValue(f.gunluk_getiri / 100);
    }
    yazilan++;
  });

  // Veri tarihi ve güncelleme zamanı, hangi günün fiyatına baktığın belli olsun.
  sayfa.getRange(FIYAT_SATIRI, 1).setNote(
    'Veri tarihi: ' + veri.veri_tarihi +
    '\nGuncelleme: ' + Utilities.formatDate(new Date(), 'Europe/Istanbul', 'dd.MM.yyyy HH:mm') +
    '\nYazilan fon: ' + yazilan
  );
}

/** Tabloya "Portföy" menüsü ekler, elle tetiklemek için. */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Portföy')
    .addItem('Fon fiyatlarını güncelle', 'fiyatlariGuncelle')
    .addToUi();
}
