from docx import Document
from docx.shared import Inches,Pt,RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT,WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.enum.style import WD_STYLE_TYPE
from datetime import date
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"BISTBOT_Proje_Kullanim_ve_Karar_Motoru_Rehberi.docx"
BLUE="1F4E78"; DARK="18324A"; LIGHT="E8EEF5"; PALE="F4F6F9"; RED="9B1C1C"; GOLD="7A5A00"; GREEN="2E6B4F"; GRAY="59636E"

def font(run,size=11,bold=False,color="222222",italic=False):
    run.font.name="Calibri"; run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"),"Calibri")
    run._element.rPr.rFonts.set(qn("w:hAnsi"),"Calibri"); run.font.size=Pt(size); run.bold=bold
    run.italic=italic; run.font.color.rgb=RGBColor.from_string(color); return run

def shade(cell,fill):
    tcPr=cell._tc.get_or_add_tcPr(); shd=tcPr.find(qn("w:shd"))
    if shd is None: shd=OxmlElement("w:shd"); tcPr.append(shd)
    shd.set(qn("w:fill"),fill)

def margins(cell,top=80,start=120,bottom=80,end=120):
    tc=cell._tc.get_or_add_tcPr(); tcMar=tc.first_child_found_in("w:tcMar")
    if tcMar is None: tcMar=OxmlElement("w:tcMar"); tc.append(tcMar)
    for edge,value in (("top",top),("start",start),("bottom",bottom),("end",end)):
        node=tcMar.find(qn(f"w:{edge}"))
        if node is None: node=OxmlElement(f"w:{edge}"); tcMar.append(node)
        node.set(qn("w:w"),str(value)); node.set(qn("w:type"),"dxa")

def table(doc,headers,rows,widths=None):
    t=doc.add_table(rows=1,cols=len(headers)); t.alignment=WD_TABLE_ALIGNMENT.CENTER; t.autofit=False
    for i,h in enumerate(headers):
        c=t.rows[0].cells[i]; shade(c,BLUE); margins(c); c.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
        p=c.paragraphs[0]; p.paragraph_format.space_after=Pt(0); font(p.add_run(str(h)),9,bold=True,color="FFFFFF")
    for ri,row in enumerate(rows):
        cells=t.add_row().cells
        for i,value in enumerate(row):
            c=cells[i]; margins(c); c.vertical_alignment=WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if ri%2: shade(c,"F7F9FB")
            p=c.paragraphs[0]; p.paragraph_format.space_after=Pt(0); font(p.add_run(str(value)),9)
    if widths:
        for row in t.rows:
            for i,w in enumerate(widths): row.cells[i].width=Inches(w)
    for row in t.rows:
        trPr=row._tr.get_or_add_trPr(); cant=OxmlElement("w:cantSplit"); trPr.append(cant)
    t.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    doc.add_paragraph().paragraph_format.space_after=Pt(0)
    return t

def para(doc,text="",bold_prefix=None,style=None):
    p=doc.add_paragraph(style=style); p.paragraph_format.space_after=Pt(6); p.paragraph_format.line_spacing=1.25
    if bold_prefix and text.startswith(bold_prefix):
        font(p.add_run(bold_prefix),bold=True); font(p.add_run(text[len(bold_prefix):]))
    else: font(p.add_run(text))
    return p

def bullet(doc,text,level=0):
    p=doc.add_paragraph(style="List Bullet" if level==0 else "List Bullet 2")
    p.paragraph_format.space_after=Pt(4); p.paragraph_format.line_spacing=1.2; font(p.add_run(text)); return p

def number(doc,text):
    p=doc.add_paragraph(style="List Number"); p.paragraph_format.space_after=Pt(5); font(p.add_run(text)); return p

def callout(doc,title,text,color=BLUE):
    t=doc.add_table(rows=1,cols=1); t.alignment=WD_TABLE_ALIGNMENT.CENTER; t.autofit=False; c=t.cell(0,0); c.width=Inches(6.3)
    trPr=t.rows[0]._tr.get_or_add_trPr(); trPr.append(OxmlElement("w:cantSplit"))
    shade(c,PALE); margins(c,140,180,140,180); p=c.paragraphs[0]; p.paragraph_format.space_after=Pt(3)
    font(p.add_run(title+"\n"),11,bold=True,color=color); font(p.add_run(text),10.5)
    doc.add_paragraph().paragraph_format.space_after=Pt(0)

def page_break(doc): doc.add_page_break()

doc=Document(); sec=doc.sections[0]
sec.page_width=Inches(8.5); sec.page_height=Inches(11); sec.top_margin=sec.bottom_margin=Inches(.78)
sec.left_margin=sec.right_margin=Inches(.85); sec.header_distance=sec.footer_distance=Inches(.42)
styles=doc.styles
normal=styles["Normal"]; normal.font.name="Calibri"; normal.font.size=Pt(11); normal.font.color.rgb=RGBColor.from_string("222222")
normal.paragraph_format.space_after=Pt(6); normal.paragraph_format.line_spacing=1.25
for name,size,color,before,after in (("Title",30,DARK,0,8),("Subtitle",14,GRAY,0,12),("Heading 1",18,BLUE,18,9),("Heading 2",14,BLUE,14,7),("Heading 3",12,DARK,10,5)):
    s=styles[name]; s.font.name="Calibri"; s.font.size=Pt(size); s.font.bold=name!="Subtitle"; s.font.color.rgb=RGBColor.from_string(color)
    s.paragraph_format.space_before=Pt(before); s.paragraph_format.space_after=Pt(after); s.paragraph_format.keep_with_next=True
for lname in ("List Bullet","List Bullet 2","List Number"):
    styles[lname].font.name="Calibri"; styles[lname].font.size=Pt(11); styles[lname].paragraph_format.space_after=Pt(4)

# Header/footer
h=sec.header.paragraphs[0]; h.alignment=WD_ALIGN_PARAGRAPH.RIGHT; font(h.add_run("BISTBOT • PAPER TRADING TEKNİK REHBERİ"),8.5,bold=True,color=GRAY)
f=sec.footer.paragraphs[0]; f.alignment=WD_ALIGN_PARAGRAPH.CENTER; font(f.add_run("Yalnızca PAPER kullanım • Yatırım tavsiyesi değildir  |  "),8,color=GRAY)
fld=OxmlElement("w:fldSimple"); fld.set(qn("w:instr"),"PAGE"); f._p.append(fld)

# Cover
p=doc.add_paragraph(); p.paragraph_format.space_before=Pt(110); p.alignment=WD_ALIGN_PARAGRAPH.CENTER
font(p.add_run("BISTBOT"),34,bold=True,color=DARK)
p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; font(p.add_run("Proje Kullanım ve Karar Motoru Rehberi"),20,bold=True,color=BLUE)
p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; font(p.add_run("Kurulum • Veri Akışı • Puanlama • Market Regime • Risk • PAPER İşlemler"),12,color=GRAY)
doc.add_paragraph().paragraph_format.space_before=Pt(50)
callout(doc,"Belgenin amacı","Bu rehber, BISTBOT’un her ana bileşenini, hangi veriyi neden kullandığını, bir hisseyi hangi koşullarda BUY/HOLD/SELL değerlendirdiğini ve Risk Engine’in emri nasıl onayladığını açıklar. Kod ve config.yaml, bu belgeden daha güncel olabilir; canlı davranışta her zaman kod ve aktif konfigürasyon esas alınır.")
p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER; p.paragraph_format.space_before=Pt(60)
font(p.add_run(f"Sürüm: PAPER v1.0-aggressive  •  Hazırlanma: {date.today().strftime('%d.%m.%Y')}"),10,color=GRAY)
page_break(doc)

doc.add_heading("İçindekiler ve hızlı yönlendirme",0)
table(doc,["Bölüm","Ne anlatır?"],[
    ("1–3","Sistem özeti, güvenlik sınırı, kurulum ve komutlar"),("4–6","Mimari, çalışma döngüsü, piyasa verisi ve scanner"),
    ("7–9","KAP/haber materyalitesi, LLM ve nihai hisse puanı"),("10","Gerçek makro pipeline ve Market Regime overlay"),
    ("11–14","Karar, Risk Engine, pozisyon büyüklüğü, BIST takvimi ve PAPER broker"),("15–17","Çıkışlar, veritabanı, dashboard ve teşhis"),
    ("18–21","Örnekler, ayarlar, hata durumları ve operasyon kontrol listesi")],[1.1,5.2])
callout(doc,"En kısa cevap","Bot doğrudan “haber iyi, al” mantığıyla çalışmaz. Önce OHLCV taraması yapar; materyal şirket olaylarını filtreler; gerekliyse LLM’den sınırlı yorum alır; makro rejimi ayrı overlay uygular; sonra Risk Engine nakit, stop, kayıp limitleri ve pozisyon sınırlarıyla son kararı verir.",GREEN)

doc.add_heading("1. Sistem nedir, ne değildir?",0)
para(doc,"BISTBOT, Borsa İstanbul hisselerini deterministik teknik göstergelerle tarayan, KAP/haber kanıtlarını sınırlı biçimde değerlendiren, geniş piyasa riskini ayrı bir Market Regime katmanıyla uygulayan ve yalnızca sanal portföyde işlem yapan bir PAPER trading uygulamasıdır.")
doc.add_heading("Temel ilkeler",1)
for x in ["PAPER ONLY: Gerçek broker bağlantısı ve gerçek para emri yoktur.","Risk Engine son otoritedir; LLM miktar belirleyemez, limit değiştiremez veya emir gönderemez.","Şirket haberi puanı ile genel makro risk birbirine karıştırılmaz.","Eksik veya bayat veri olumlu varsayımla doldurulmaz; aday elenir ya da giriş engellenir.","Açık pozisyon çıkış kontrolü, yeni giriş taramasından önce yapılır."]: bullet(doc,x)
callout(doc,"Uyarı","Bu yazılım yatırım tavsiyesi üretmez. Geçmiş veya sanal performans gerçek piyasadaki sonucu garanti etmez.",RED)

doc.add_heading("2. Hızlı kurulum",0)
doc.add_heading("Gereksinimler",1)
for x in ["Python 3.12 veya üzeri","İnternet bağlantısı (gerçek piyasa, KAP/haber ve makro kaynaklar için)","OpenAI kullanılacaksa proje kökündeki .env dosyasında OPENAI_API_KEY","Yazılabilir SQLite veritabanı konumu"]: bullet(doc,x)
doc.add_heading("Kurulum komutları",1)
for x in ["python -m venv .venv","source .venv/bin/activate","pip install -r requirements.txt"]: para(doc,x,style="Intense Quote")
para(doc,"Aktif ayar dosyası varsayılan olarak proje kökündeki config.yaml’dır. Veritabanı göreli yazılmışsa proje köküne göre çözülür.")

doc.add_heading("3. Çalıştırma komutları",0)
table(doc,["Komut","Davranış","Emir?"],[
    ("python main.py","Scheduler ile sürekli PAPER döngüsü","Sanal PAPER"),("python main.py --once","Tek tam döngü","Sanal PAPER"),
    ("python main.py --dry-run","Hesaplar; yeni kalıcı işlem yapmaz","Hayır"),("python main.py --once --verbose","Tek döngü + ayrıntılı teşhis","Sanal PAPER"),
    ("python main.py --status","Portföy/pozisyon durumunu okur","Hayır"),("python main.py --report","Portföy modelini raporlar","Hayır"),
    ("python main.py --analyze-symbol THYAO","Tek hisseyi salt-okunur analiz eder","Hayır"),
    ("python main.py --diagnose-intelligence","Haber/KAP/OpenAI kaynaklarını sınar","Hayır"),
    ("python main.py --diagnose-regime","Makro kaynakları ve rejimi sınar","Hayır")],[2.25,3.35,.7])
callout(doc,"Dikkat: --reset","Bu komut veritabanı dosyasını silerek PAPER portföyünü sıfırlar. Normal kullanım veya teşhis için gerekli değildir. Mevcut pozisyon ve işlem geçmişi korunmak isteniyorsa çalıştırılmamalıdır.",RED)
doc.add_heading("4. Mimari ve veri akışı",0)
for i,x in enumerate(["Açık pozisyonları al ve güncel fiyatlarla çıkış koşullarını kontrol et.","Aktif BIST sembol evrenini yükle ve OHLCV verisini çek.","Likidite filtresini geçen hisseler için teknik scanner puanları üret.","En yüksek scanner adaylarına KAP ve haber olaylarını bağla; materyaliteyi deterministik filtrele.","Yalnızca kanıt bulunan materyal olayları LLM’ye gönder; sonuç yoksa teknik-only devam et.","Market Regime katmanını yenile; piyasa ve sektör ayarını hisse puanına overlay olarak uygula.","BUY/SELL önerisini Risk Engine’e gönder; miktar ve güvenlik limitlerini burada sonlandır.","Risk onaylarsa PaperBroker sanal fill, komisyon, slippage, nakit ve pozisyon kayıtlarını günceller.","Döngü özeti, karar açıklamaları ve dashboard verileri SQLite’a yazılır."],1): number(doc,x)
callout(doc,"Öncelik kuralı","Açık pozisyonların korunması yeni fırsat aramadan önce gelir. Açık pozisyon fiyatı bayatsa yeni girişler bloke edilir; sistem eski fiyatı güncelmiş gibi kabul etmez.",GOLD)

doc.add_heading("5. Piyasa verisi ve sembol evreni",0)
para(doc,"Gerçek çalışma modunda YahooBistProvider kullanılır. Aktif semboller önce KAP aktif şirket listesinden yüklenmeye çalışılır; bu kaynak çalışmazsa bakımı yapılan data/bist_symbols.txt dosyası kullanılır. Semboller .IS biçimine normalize edilir.")
doc.add_heading("Veri kalite kontrolleri",1)
for x in ["Minimum bar sayısı: 22","Bar zamanları kesin artan sırada olmalı","OHLC ilişkileri geçerli olmalı; sayılar sonlu olmalı","İşlem saatinde varsayılan maksimum veri yaşı 30 dakika","Minimum ortalama hacim: 100.000","Minimum ortalama işlem tutarı: 1.000.000 TL","Bir sembolün hatası tüm batch’i düşürmez; sembol bazında izole edilir"]: bullet(doc,x)

doc.add_heading("6. Teknik scanner nasıl puanlar?",0)
para(doc,"Scanner yalnızca OHLCV kullanır; bu aşamada LLM çağrılmaz. Önce göstergeler hesaplanır, likit olmayan hisse elenir, sonra 0–100 arası bileşenler oluşturulur.")
table(doc,["Bileşen","Hesap mantığı","Aktif ağırlık"],[
    ("Technical","EMA9/EMA21 farkı, son zirve kırılımı/mesafesi ve RSI ayarı","%25"),
    ("Momentum","1, 3 ve 12 bar getirilerinin ağırlıklı etkisi","%30"),("Volume","20 + göreli hacim × 30, 0–100 sınırı","%25"),
    ("Liquidity","Ortalama hacim %40 + turnover %45 + süreklilik %15","%10"),
    ("Volatility","Hedef %1,5 oynaklığa uzaklık; yüksek oynaklık cezası","%10")],[1.05,4.25,1.0])
doc.add_heading("Temel formüller",1)
for x in ["momentum_score = clamp(50 + 3×getiri_1 + 2×momentum_3 + momentum_12)","trend_score = clamp(50 + 7×yüzdesel(EMA9, EMA21))","technical_score = clamp(50 + 7×trend_spread + breakout/zirve etkisi + RSI etkisi)","volume_score = clamp(20 + 30×relative_volume)","scanner_score = Σ(bileşen_puanı × scanner_ağırlığı)"]: para(doc,x,style="Intense Quote")
para(doc,"RSI 50–70 aralığı +10 destek verir. RSI 70–80 arası ceza alır; RSI ≥80 olduğunda -25 uygulanır ve technical_score en fazla 70 olabilir. Breakout, kapanışın önceki lookback zirvesini aşmasıdır.")
callout(doc,"Önemli config notu","Eski nested config yapısındaki scanner.weights.trend alanı uyumluluk katmanında volatility ağırlığına dönüştürülür. Ayrı trend_score yine hesaplanır ve nihai hisse puanında gerçek katkı olarak kullanılır.",GOLD)

doc.add_heading("7. KAP, haber ve materyalite",0)
para(doc,"Scanner’ın üst sıralarındaki adaylar için KAP ve finans haberleri çekilir. Aynı olay hash ile tekilleştirilir. Kaynak güveni, güncellik, sembol ilgisi, anahtar temalar ve materyalite birlikte event_score üretir.")
table(doc,["Event-score bileşeni","Ağırlık"],[('Güncellik (24 saat half-life)','%25'),('Kaynak güveni','%25'),('Sembol ilgisi','%20'),('Anahtar kelime temaları','%15'),('Materyalite','%15')],[4.9,1.4])
para(doc,"KAP güven skoru 95, genel haber güven skoru 65’tir. Devre kesici ve rutin MKK bildirimleri güvenilir kaynakta yayımlansa bile düşük materyalite sayılır. Başlık-only sözleşme haberi yüksek güvenli karar için yeterli değildir.")
callout(doc,"LLM maliyet kontrolü","Şirket olayı materiality_score 50 altındaysa LLM’ye gönderilmez. Haber yoksa veya olay yetersizse LLM NOT_REQUIRED/HOLD üretir ve puanlama mevcut bileşenlerle yeniden normalize edilir.",GREEN)

doc.add_heading("8. LLM’nin rolü ve sınırları",0)
for x in ["Yalnızca verilen teknik veri ve doğrulanmış olayları kullanır.","Kaynak ID uyduramaz; çıktıdaki source_ids giriş kümesinde olmalıdır.","BUY/HOLD/SELL yön eğilimi, katalizör, fiyatlanma olasılığı, risk ve güven döndürür.","Belirsiz başlıkları şirket lehine yorumlayamaz.","Pozisyon büyüklüğü, risk limiti veya gerçek/sanal emir kararı veremez.","Model hatası, şema hatası veya erişim sorunu güvenli HOLD/UNAVAILABLE sonucuna döner."]: bullet(doc,x)
table(doc,["LLM alanı","Aralık/anlam"],[('sentiment','-100…+100'),('importance / catalyst_score / risk_score / confidence','0…100'),('priced_in_probability','0…100'),('time_horizon','intraday / swing / none'),('action_bias','BUY / SELL / HOLD')],[3.6,2.7])

page_break(doc)
doc.add_heading("9. Nihai hisse puanı",0)
para(doc,"Nihai strateji puanı scanner_score ile aynı değildir. Teknik sinyalin alt bileşenleri, şirket olayı ve varsa LLM analizi yeniden bir araya getirilir. Mevcut aktif ağırlıklar aşağıdadır.")
table(doc,["Nihai bileşen","Ağırlık","Not"],[
    ("Technical","%12","Toplam teknik ağırlığın %60’ı"),("Trend","%4","Toplam teknik ağırlığın %20’si"),
    ("Liquidity","%4","Toplam teknik ağırlığın %20’si"),("Momentum","%25","Doğrudan"),("Volume","%20","Doğrudan"),
    ("News/KAP","%20","Yalnız materyal olay varsa"),("LLM","%15","Yalnız geçerli analiz varsa")],[1.35,.8,4.15])
para(doc,"Kullanılmayan News/KAP veya LLM ağırlığı sıfır puan gibi eklenmez. Mevcut ağırlıklar toplamı 1 olacak şekilde yeniden normalize edilir. Trend ve liquidity her teknik sinyalde mevcuttur ve artık missing_components içinde gösterilmez.")
doc.add_heading("LLM puan dönüşümü",1)
for x in ["BUY bias: 50 + catalyst_score × confidence / 200","SELL bias: confidence (bearish hesap ayrıca teknik bileşenleri tersler)","HOLD bias: LLM mevcutsa yeni BUY’ı bloke eder"]: bullet(doc,x)
para(doc,"Temel BUY eşiği aktif config’te 72, SELL eşiği 38’dir. strong_buy_threshold 82 config’te tanımlı olsa da mevcut strateji karar kodu BUY için buy_threshold değerini kullanır.")

doc.add_heading("10. Market Regime / gerçek makro pipeline",0)
para(doc,"Makro katman şirket puanına sıradan haber bileşeni olarak eklenmez. Ayrı risk overlay’i olarak çalışır. Varsayılan sağlayıcı RealMacroNewsProvider’dır; STATIC yalnızca açık test enjeksiyonunda kullanılır.")
doc.add_heading("Kaynaklar",1)
for x in ["Resmî RSS: TCMB, Federal Reserve, ECB","Piyasa bağlamı: USDTRY, EURTRY, Brent, Gold, S&P 500, Nasdaq, VIX, ABD 10Y","SPK, Borsa İstanbul, TÜİK ve Hazine için kararlı makine-okunur endpoint yoksa UNAVAILABLE raporlanır; veri uydurulmaz","Sosyal medya doğrulama kaynağı değildir"]: bullet(doc,x)
doc.add_heading("Yedi risk kategorisi",1)
table(doc,["Kategori","Örnek kapsam"],[('turkey_macro_risk','Enflasyon, TCMB, Türkiye makro şoku'),('domestic_policy_risk','SPK/BIST düzenleme, vergi/politika'),('geopolitical_risk','Savaş ve bölgesel gerilim'),('global_market_risk','Fed/ECB, küresel endeks şoku'),('fx_risk','USDTRY/EURTRY şoku'),('commodity_risk','Brent/altın şoku'),('systemic_event_risk','Deprem, bankacılık veya sistem olayı')],[2.1,4.2])
doc.add_heading("Deterministik makro materyalite sınıfları",1)
table(doc,["Event type","Ham materyalite","Örnek"],[
    ('FX_CAPITAL_CONTROL_POLICY','98','Sermaye/döviz kontrolü'),('CENTRAL_BANK_RATE_DECISION','95','TCMB faiz duyurusu, FOMC statement'),
    ('BANKING_SYSTEM_REGULATION','88','Bankacılık sistemi düzenlemesi'),('MACROPRUDENTIAL_POLICY','86','Makroihtiyati çerçeve'),
    ('LIQUIDITY_POLICY','82','TL likidite yönetimi'),('INFLATION_REPORT','80','Enflasyon raporu'),
    ('ECONOMIC_PROJECTIONS','78','Fed/merkez bankası projeksiyonları'),('CENTRAL_BANK_MINUTES','76','PPK/FOMC tutanak veya özetleri'),
    ('SPEECH_INTERVIEW','45','Konuşma veya röportaj'),('DIGITAL_CURRENCY_OPERATIONAL','30','Operasyonel dijital para haberi'),
    ('PAYMENT_SYSTEM_ADMIN','22','Ödeme sistemi idari duyurusu'),('ACADEMIC_CEREMONIAL','7','Makale yarışması/tören'),
    ('PUBLIC_EVENT','3','Konser veya halka açık etkinlik')],[2.55,1.15,2.6])
para(doc,"Kaynak güveni materyalitenin içine karıştırılmaz. Ham materyalite konunun piyasa önemini; source_reliability kaynağın güvenilirliğini; confirmation_score ise doğrulama gücünü temsil eder.")
doc.add_heading("Timestamp ve freshness kalitesi",1)
table(doc,["timestamp_source","Anlam","Rejim davranışı"],[
    ('SOURCE','Kaynağın doğrudan yayın zamanı','Normal freshness hesabı'),('PARSED_PAGE','Kaynak sayfasından deterministik ayrıştırıldı','Normal freshness hesabı'),
    ('FETCH_FALLBACK','Yayın zamanı bulunamadı; fetched_at kullanıldı','Freshness 0; breaking event sayılmaz')],[1.55,2.7,2.05])
table(doc,["Olay yaşı","Freshness weight"],[('<6 saat','1,00'),('6–24 saat','0,85'),('1–3 gün','0,60'),('3–7 gün','0,25'),('>7 gün','0,05 / background')],[4.6,1.7])
para(doc,"effective_materiality = raw_materiality × freshness_weight × reliability_factor × confirmation_factor. Mixed yönlü politika olaylarının regime_contribution değeri efektif materyalitenin %75’i; açık negatif olayların %100’üdür. Ham materyalite ≥40 ve efektif materyalite ≥10 olmayan olaylar güncel rejime girmez.")
para(doc,"Risk skoru, negatif/mixed kategorilerdeki en yüksek regime_contribution + ikinci en yüksek katkının %20’si olarak hesaplanır ve 100’de sınırlandırılır. Böylece eski FOMC/faiz kararları yüksek ham materyalite taşısa bile güncel rejimi süresiz yüksek tutamaz. Makro LLM kapısı config’te 70, refresh süresi 45 dakikadır.")
table(doc,["Rejim","Risk/koşul","Skor ayarı","BUY eşiği","Pozisyon"],[
    ('RISK_ON','Pozitif materyal sinyal, risk <40','+2','+0','1,00×'),('NORMAL','Risk <40','0','+0','1,00×'),
    ('CAUTION','Risk 40–69','-4','+3','0,75×'),('RISK_OFF','Risk 70–89','-10','+8','0,40×'),
    ('CRISIS','Risk ≥90','0; giriş blok','—','0×')],[1.0,1.55,1.0,1.0,1.0])
para(doc,"adjusted_final_score = clamp(base_stock_score + market_adjustment + sector_adjustment). adjusted_buy_threshold = 72 + buy_threshold_adjustment.")
doc.add_heading("Sektör etkisi",1)
para(doc,"Sembol deterministik sektör eşlemesinde yer alıyorsa ve olay o sektörü pozitif/negatif işaretliyorsa ±2 puan uygulanır. Örnek eşlemeler: THYAO/PGSUS airlines; TUPRS/AYGAZ/PETKM energy; ASELS/OTKAR defense; büyük bankalar banks. Eşlenmemiş şirkete sektör etkisi uygulanmaz ve şirket özel maruziyeti kanıtsız çıkarılmaz.")

doc.add_heading("11. BUY, HOLD ve SELL kararı",0)
table(doc,["Karar","Ana koşul"],[
    ('BUY','LLM HOLD ile bloklamıyorsa ve overlay sonrası skor, ayarlanmış BUY eşiğine eşit/üzerindeyse'),
    ('HOLD','Skor eşik altında; LLM HOLD; rejim giriş bloklu; pozisyon zaten açık; açık pozisyon verisi bayat; Risk reddi'),
    ('SELL','Açık pozisyon çıkış kuralı veya bearish strateji değerlendirmesi Risk Engine tarafından onaylanırsa')],[1.1,5.2])
para(doc,"Aynı sembolde açık PAPER pozisyonu varsa pyramiding kapalıdır. Yeni BUY yerine MANAGE_EXISTING_POSITION/HOLD açıklaması gösterilir.")
callout(doc,"Karar açıklaması sözleşmesi","Her adayda base_stock_score, market_regime, market_adjustment, sector_adjustment, adjusted_final_score, adjusted_buy_threshold ve position_multiplier gösterilir. Böylece nihai kararın nereden geldiği izlenebilir.",GREEN)

doc.add_heading("12. Risk Engine: son otorite",0)
para(doc,"Strateji BUY dese bile emir kesin değildir. Risk Engine aşağıdaki kontrollerden sonra APPROVE, REJECT, REDUCE_SIZE veya HALT_TRADING üretir.")
table(doc,["Kontrol","Aktif sınır"],[('Global kill switch','Aktifse BUY durur'),('Maksimum açık pozisyon','7'),('Maksimum tek pozisyon','Özkaynağın %22’si'),('Minimum nakit rezervi','Özkaynağın %5’i'),('İşlem başına maksimum risk','Özkaynağın %1,25’i'),('Günlük zarar','Başlangıç sermayesinin %4’ü'),('Haftalık zarar','Başlangıç sermayesinin %8’i'),('Toplam drawdown','%15'),('Fiyat yaşı','En fazla 5 dakika'),('Stop doğrulama','0 < stop < giriş')],[3.2,3.1])

doc.add_heading("13. Pozisyon büyüklüğü",0)
para(doc,"Risk Engine üç ayrı üst sınır hesaplar ve en küçüğünü kullanır:")
for x in ["by_position = floor((equity × max_position_pct − mevcut_pozisyon_değeri) / giriş_fiyatı)","by_risk = floor((equity × max_trade_risk_pct) / (giriş_fiyatı − stop_fiyatı))","by_cash = floor((cash − equity × min_cash_pct) / giriş_fiyatı)","quantity = min(by_position, by_risk, by_cash)"]: para(doc,x,style="Intense Quote")
para(doc,"RISK_OFF gibi rejimlerde Risk Engine’in güvenli bulduğu BUY miktarı ayrıca yalnızca aşağı yönde position_multiplier ile azaltılır. Örneğin Risk Engine 100 adet onaylar ve çarpan 0,40 ise PAPER talebi 40 adede indirilir. CRISIS’te strateji Risk Engine’e yeni BUY göndermeden HOLD olur.")

page_break(doc)
doc.add_heading("14. BIST işlem takvimi ve seans güvenliği",0)
para(doc,"BistTradingCalendar, Europe/Istanbul saat diliminde Pay Piyasası işlem gününü ve seansını belirleyen tek otoritedir. Strateji veya Risk Engine BUY/SELL üretse bile PaperBroker, seans uygun değilse fill öncesinde emri bağımsız olarak reddeder; nakit, pozisyon, order ve fill kayıtları değişmez.")
table(doc,["Seans","V1 PAPER emri?","Yaklaşık normal gün aralığı"],[("CLOSED / POST_CLOSE","Hayır","09:40 öncesi / 18:10 sonrası"),("PRE_OPEN","Hayır","09:40–09:55"),("OPENING_AUCTION","Hayır","09:55–10:00"),("CONTINUOUS_TRADING","Evet","10:00–18:00"),("CLOSING_AUCTION","Hayır","18:00–18:10")],[2.2,1.6,2.5])
para(doc,"2026 tam ve yarım gün tatilleri Borsa İstanbul’un resmî Pay Piyasası tablosundan açık tarih listesi olarak tutulur. Yarım günlerde sürekli işlem 12:30’da, kapanış süreci 12:40’ta biter. Desteklenmeyen takvim yılı MARKET_CALENDAR_UNAVAILABLE ile güvenli biçimde kapalı kabul edilir; hafta sonu MARKET_CLOSED_WEEKEND, tam tatil MARKET_CLOSED_HOLIDAY üretir.")
for x in ["Kapalı piyasada haber/KAP/makro toplama, rejim yenileme, teşhis, isteğe bağlı scanner ve son kapanışla değerleme devam edebilir.","BUY/SELL, stop-loss, take-profit, trailing-stop, strategy-exit ve time-stop fill’i yapılamaz.","Cuma kapanışı hafta sonu LIVE fiyat değildir; LAST_CLOSE / MARKET_CLOSED etiketiyle yalnızca gösterim/değerleme içindir.","Kapalı piyasada üretilen sinyal kuyruğa alınmaz. Sonraki açık seansta taze veriyle scanner, intelligence, rejim, strateji ve risk yeniden hesaplanır."]: bullet(doc,x)
callout(doc,"Broker sınırı","Strategy → BUY ve Risk → APPROVED olsa bile PaperBroker → BLOCKED / MARKET_CLOSED sonucunu verebilir. Bu kontrol broker seviyesinde atlanamaz.",RED)
doc.add_heading("Kapalı piyasa LLM politikası",1)
para(doc,"Piyasa kapalıyken haber ve makro kaynakları yenilenebilir; OpenAI kullanımı scheduler’a değil yeni olaya bağlıdır. Şirket LLM’i yalnız daha önce kalıcı cache’de görülmemiş materyal event hash’i için çağrılır. Teknik skor değişse bile aynı event yeniden istek üretmez. Makro LLM yalnız yeni canonical event, eşik üstü deterministik materyalite ve yeterli freshness varsa çalışır; değişmeyen olaylar kalıcı cache’den okunur.")
para(doc,"Her döngü market_closed, company_llm_calls, macro_llm_calls ve llm_calls_avoided_by_cache sayaçlarını raporlar. Rutin hafta sonu polling’i yeni olay yoksa sıfır OpenAI çağrısıyla tamamlanır.")
doc.add_heading("Piyasa açılış geçişi ve fiyat güvenliği",1)
para(doc,"Hafif session heartbeat her 60 saniyede yalnız takvimi kontrol eder ve OpenAI çağırmaz. CLOSED/PRE_OPEN/OPENING_AUCTION durumundan CONTINUOUS_TRADING’e geçiş algılanırsa normal 600 saniyelik aralık beklenmeden tam piyasa döngüsü başlatılır.")
for x in ["Taze piyasa verisi çekilir ve timestamp doğrulanır.","Açık pozisyonlar yenilenir; stop/TP/trailing yeniden değerlendirilir.","Scanner, intelligence, rejim, Strategy ve Risk güncel verilerle yeniden çalışır.","Yalnız aynı işlem gününde 10:00 sonrasında oluşmuş ve en fazla 5 dakikalık fiyat PAPER fill için kullanılabilir.","Cuma kapanışı veya pre-open bayat veri NO_FRESH_SESSION_PRICE ile engellenir; sonraki döngüde tekrar taze veri aranır."]: bullet(doc,x)

doc.add_heading("15. PAPER broker ve maliyetler",0)
para(doc,"PaperBroker tek execution adapter’ıdır. Gerçek broker API’si yoktur. BUY’da slippage fiyatı yukarı, SELL’de aşağı taşır; komisyon nakitten düşülür. Mevcut aktif değerler komisyon %0,10, slippage %0,15’tir.")
for x in ["Sanal BUY fill: piyasa fiyatı × (1 + slippage)","Sanal SELL fill: piyasa fiyatı × (1 − slippage)","Fill, order, komisyon, realized P&L ve pozisyon SQLite’a kaydedilir","Yetersiz nakit veya geçersiz RiskDecision ile fill oluşturulmaz"]: bullet(doc,x)

doc.add_heading("16. Açık pozisyon çıkışları",0)
table(doc,["Çıkış","Aktif kural"],[('Hard stop','Giriş fiyatının %5 altı'),('Take profit','Giriş fiyatının %10 üstü'),('Trailing stop','Görülen en yüksek fiyatın %4,5 altı'),('Strategy exit','Teknik skor SELL eşiği 38 veya altı'),('Time stop','Ayar varsa maksimum holding günü')],[2.0,4.3])
para(doc,"Döngünün ilk adımında her açık pozisyonun fiyatı ve yüksek seviyesi yenilenir. Bayat fiyatla çıkış varsayımı yapılmaz; durum STALE_MARKET_DATA olarak gösterilir. Yeni girişler de açık pozisyonlardan biri bayatsa engellenir.")

doc.add_heading("17. Veritabanı ve dashboard",0)
para(doc,"SQLite; teknik sinyalleri, event’leri, LLM cache’ini, makro event/cache ve rejim durumlarını, risk kararlarını, PAPER order/fill/pozisyonları, portföy snapshot’larını ve sistem olaylarını saklar. Uygulama açılışında eksik şema alanları güvenli migration ile eklenir; normal çalışma geçmişi silmez.")
doc.add_heading("Dashboard bölümleri",1)
for x in ["BIST Market: OPEN/CLOSED, seans, gerekçe, İstanbul saati ve emir izni","Canlı özet: equity, nakit, realized/unrealized P&L","Market Regime: provider, rejim, risk, güven, yedi kategori, materyal olaylar","Açık pozisyonlar: LIVE veya LAST_CLOSE / MARKET_CLOSED fiyat etiketi, skorlar ve çıkış seviyeleri","Trade history ve kapanmış pozisyonlar","Bot activity: son cycle özeti ve kararlar","Risk oranları ve limitler"]: bullet(doc,x)
para(doc,"Dashboard salt-okunur projeksiyondur; kullanıcıya emir butonu sunmaz. Açık pozisyon fiyatları display-only yenilenebilir; bu işlem bot döngüsü veya OpenAI çağrısı başlatmaz.")

doc.add_heading("18. Uçtan uca örnek BUY/HOLD",0)
table(doc,["Adım","Örnek"],[('Temel hisse puanı','78'),('Rejim','CAUTION'),('Market adjustment','−4'),('Sektör adjustment','−2'),('Ayarlanmış skor','72'),('Ayarlanmış BUY eşiği','72 + 3 = 75'),('Strateji kararı','HOLD')],[2.5,3.8])
para(doc,"Aynı temel skor NORMAL rejimde, sektör etkisi yoksa 78 kalır ve 72 eşiğini geçtiği için strateji BUY önerebilir. Ancak gerçek PAPER fill için Risk Engine’in stop, fiyat tazeliği, nakit, kayıp ve pozisyon kontrollerini de geçmesi gerekir.")

doc.add_heading("19. Teşhis ve sorun giderme",0)
table(doc,["Belirti","Kontrol"],[
    ('Macro provider PARTIAL','--diagnose-regime içinde sources alanında hangi endpoint’in erişilemediğine bak'),
    ('NORMAL / risk 0','events_material=0 ise normaldir; veri yokmuş gibi uydurma risk üretilmez'),
    ('LLM calls 0','Materyal olay yoktur, cache vardır veya API/provider kapalıdır'),
    ('Hep HOLD','Adjusted score/eşik, LLM HOLD, stale position, mevcut pozisyon veya Risk reddini incele'),
    ('Market data eksik','Sembol normalize, KAP evreni, Yahoo endpoint ve stale timestamp kontrolü'),
    ('missing news_kap/llm','O döngüde geçerli kanıt yoksa beklenen davranıştır; trend/liquidity missing olmamalı'),
    ('MARKET_CLOSED_WEEKEND','Sinyal hesaplanabilir ancak PaperBroker fill üretmez; pazartesi otomatik tekrar oynatılmaz'),
    ('MARKET_CALENDAR_UNAVAILABLE','Takvim yılı doğrulanmamıştır; execution güvenli şekilde kapalıdır'),
    ('Yeni giriş yok','Açık pozisyon stale, maksimum pozisyon, nakit rezervi veya kayıp limitleri olabilir')],[2.0,4.3])
para(doc,"--diagnose-regime her olay için event_type, published_at, fetched_at, timestamp_source, age_hours, source_reliability, confirmation_score, materiality, freshness_weight, effective_materiality, llm_analyzed ve regime_contribution alanlarını gösterir.")

doc.add_heading("20. Aktif ayar özeti",0)
table(doc,["Alan","Değer"],[('Başlangıç sermayesi','200.000 TL'),('Scheduler','600 saniye'),('Scanner aday limiti','50'),('LLM aday limiti','15'),('BUY / SELL eşiği','72 / 38'),('Makro refresh','45 dakika'),('macro_llm_materiality_threshold','70'),('Sektör adjustment','±2'),('Komisyon / slippage','%0,10 / %0,15'),('Mod','PAPER')],[3.6,2.7])
callout(doc,"Ayar değişikliği","config.yaml değiştirildiğinde puanlar ve güvenlik sınırları değişebilir. Risk sınırlarını yükseltmek gerçek riski artırır; değişiklik sonrası test paketi ve --dry-run çalıştırılmalıdır.",GOLD)

doc.add_heading("21. Operasyon kontrol listesi",0)
for x in ["Başlangıçtaki BIST MARKET STATUS alanında İstanbul saati, seans ve emir iznini kontrol et.",".env ve OPENAI_API_KEY durumunu kontrol et.","python main.py --diagnose-intelligence çalıştır.","python main.py --diagnose-regime ile kaynak ve materyaliteyi kontrol et.","python main.py --dry-run ile veri/karar akışını emirsiz doğrula.","--status ile açık pozisyonlarda STALE_MARKET_DATA olup olmadığını kontrol et.","İlk PAPER döngüsünü python main.py --once --verbose ile çalıştır.","Dashboard’da BIST Market, rejim, karar açıklaması, RiskDecision ve PAPER fill kayıtlarını karşılaştır.","Üretim benzeri PAPER çalışmada --reset komutunu kullanma."]: bullet(doc,x)

doc.add_heading("Ek A — Karar alanları sözlüğü",0)
table(doc,["Alan","Anlam"],[('scanner_score','İlk OHLCV sıralama puanı'),('base_stock_score','Makro overlay öncesi nihai hisse puanı'),('market_adjustment','Rejim kaynaklı genel puan değişimi'),('sector_adjustment','Deterministik sektör eşleşmesi etkisi'),('adjusted_final_score','Overlay sonrası puan'),('adjusted_buy_threshold','Rejime göre yükseltilmiş BUY eşiği'),('position_multiplier','Risk-onaylı BUY miktarını azaltan rejim çarpanı'),('confidence','Rejim/LLM değerlendirmesinin kanıt güveni'),('reason_code','Risk Engine’in makine-okunur karar gerekçesi')],[2.2,4.1])

doc.add_heading("Ek B — Kaynak dosya haritası",0)
table(doc,["Dosya/klasör","Sorumluluk"],[('bistbot/app/runtime.py','Ana döngü ve bileşen orkestrasyonu'),('bistbot/market/calendar.py','BIST takvimi, tatil ve seans otoritesi'),('bistbot/market/scanner.py','Teknik OHLCV scanner'),('bistbot/intelligence/','KAP, haber, materyalite ve hisse LLM'),('bistbot/strategy/engine.py','Nihai hisse puanı ve BUY/HOLD/SELL'),('bistbot/market_regime/','Gerçek makro kaynak, materyalite, LLM, rejim ve sektör overlay'),('bistbot/risk/','Risk kontrolleri ve pozisyon büyüklüğü'),('bistbot/broker/paper.py','Seans korumalı sanal execution ve PAPER muhasebe'),('bistbot/storage/','SQLite şema ve repository’ler'),('dashboard.py / dashboard_data.py','Salt-okunur dashboard'),('config.yaml','Aktif çalışma parametreleri'),('tests/','Davranış ve güvenlik testleri')],[2.35,3.95])

# Keep headings with content and add core properties.
doc.core_properties.title="BISTBOT Proje Kullanım ve Karar Motoru Rehberi"
doc.core_properties.subject="PAPER trading mimarisi, puanlama, rejim ve risk açıklaması"
doc.core_properties.author="BISTBOT Project Documentation"
# Word may retain an empty paragraph after the final table and push it to a
# footer-only page. Remove trailing empty paragraphs before saving.
while doc.paragraphs and not doc.paragraphs[-1].text.strip():
    paragraph=doc.paragraphs[-1]
    paragraph._element.getparent().remove(paragraph._element)
doc.save(OUT)
print(OUT)
