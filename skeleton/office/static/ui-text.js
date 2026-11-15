// Every sentence the owner reads in the office UI, in one place, in Indonesian.
//
// Why one file: the owner is an Indonesian shop or service owner, so the whole UI speaks Indonesian
// (the page is `<html lang="id">`). Keeping the words here lets them be reviewed in one sitting and
// lets tests/js/ui-text.test.mjs check them (no English left, every placeholder filled).
//
// The file is pure data plus two tiny helpers: no DOM, no imports. The modules ui-*.js, main.js,
// office.js and characters.js take their text from `TEXT`. The static labels in index.html (the
// top bar, the policy dialog, the feed header) are written there in the same words, so the first
// paint is already Indonesian. tests/js/ui-text.test.mjs checks that those words match this file.
//
// Text with a value in it uses {name} placeholders: `fmt(TEXT.panel.titleFor, { name: "Sari" })`.

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

export const TEXT = deepFreeze({
  lang: "id",

  // ---- top bar ---------------------------------------------------------------------------
  hud: {
    today: "Hari ini",
    spendTitle: "Pengeluaran hari ini / batas belanja harian",
    spendTitleNoLimit: "Pengeluaran hari ini. Batas harian belum diatur.",
    notSet: "belum diatur",
    demo: "Mode demo",
    demoTitle: "Data contoh, bukan usaha sungguhan",
    paused: "Dijeda",
    rules: "Aturan",
    rulesTitle: "Ubah batas belanja harian dan aksi yang diblokir",
    pause: "Jeda",
    resume: "Lanjutkan",
    pauseAll: "Jeda semua staf",
    resumeAll: "Lanjutkan semua staf",
    pauseTitle: "Hentikan staf memulai pekerjaan baru",
    resumeTitle: "Biarkan staf mulai bekerja lagi",
    refreshFailed: "Tidak bisa memuat ulang kantor. {error}",
    pauseFailed: "Tidak bisa menjeda kantor. {error}",
    resumeFailed: "Tidak bisa melanjutkan kantor. {error}",
  },
  quality: {
    button: "Kualitas: {mode}",
    normal: "Normal",
    low: "Rendah",
    buttonTitle: "Ganti antara kualitas normal dan rendah",
    hint: "Pindah ke kualitas rendah?",
    hintYes: "Pindah",
    hintNo: "Nanti saja",
    hintNoLabel: "Tutup saran",
  },
  loading: {
    title: "Kantor Agen",
    label: "Memuat kantor",
    renderer: "Menyiapkan tampilan",
    floor: "Membangun lantai dan dinding",
    desks: "Menata meja",
    pantry: "Menata pantry dan meja rapat",
    decor: "Menambah dekorasi",
    meshes: "Menyusun objek",
    firstFrame: "Menggambar tampilan pertama",
    ready: "Siap",
    characters: "Memuat karakter",
    charactersReady: "Karakter siap",
  },
  fatal: {
    noWebgl: "Tidak bisa memulai kantor 3D. Browser ini memerlukan WebGL.",
    loadFailed: "Tidak bisa memuat kantor: {error}",
    noScript: "Halaman ini memerlukan JavaScript dan WebGL.",
  },
  office: { canvas: "Kantor 3D", overlay: "Staf di kantor" },

  // ---- states and statuses -----------------------------------------------------------------
  states: {
    idle: "Santai",
    working: "Bekerja",
    handoff: "Serah-terima",
    waiting: "Menunggu persetujuan",
    paused: "Dijeda",
    unknown: "Tidak diketahui",
  },
  idleActivity: {
    stretch: "meregangkan badan",
    drink: "mengambil minum",
    chat: "mengobrol dengan rekan",
  },
  status: {
    done: "Selesai",
    pending: "Menunggu",
    rejected: "Ditolak",
    failed: "Gagal",
    info: "Info",
    unknown: "Tidak diketahui",
  },

  // ---- agent tags over the 3D scene ----------------------------------------------------------
  overlay: {
    label: "{name}{role}. {detail}.{bubble} Buka detail",
    roleSeparator: ", ",
  },

  // ---- agent panel -------------------------------------------------------------------------
  panel: {
    close: "Tutup panel staf",
    closeTitle: "Tutup (Esc)",
    ariaFor: "Panel staf: {name}",
    spendToday: "Pengeluaran hari ini",
    pause: "Jeda staf",
    resume: "Lanjutkan staf",
    pauseFor: "Jeda {name}",
    resumeFor: "Lanjutkan {name}",
    officePaused: "Seluruh kantor sedang dijeda, jadi tidak ada staf yang memulai pekerjaan baru.",
    noAction: "Belum ada aksi",
    recent: "Aksi terbaru",
    recentLabel: "Aksi terbaru, terbaru di atas",
    loading: "Memuat\u2026",
    empty: "Belum ada aktivitas hari ini.",
    loadFailed: "Tidak bisa memuat data terbaru. {error}",
    pauseFailed: "Tidak bisa menjeda staf. {error}",
    resumeFailed: "Tidak bisa melanjutkan staf. {error}",
  },
  rename: {
    open: "Ubah nama",
    openLabel: "Ubah nama dan peran {name}",
    title: "Ubah nama",
    name: "Nama",
    role: "Peran",
    roleHelp: "Peran adalah tulisan di bawah nama, misalnya Kasir.",
    save: "Simpan",
    saving: "Menyimpan\u2026",
    cancel: "Batal",
    saveFailed: "Tidak bisa menyimpan nama. {error}",
    nameEmpty: "Nama tidak boleh kosong.",
    roleEmpty: "Peran tidak boleh kosong.",
    nameTooLong: "Nama terlalu panjang. Maksimal {max} huruf.",
    roleTooLong: "Peran terlalu panjang. Maksimal {max} huruf.",
    control: "Tidak boleh berisi karakter kontrol.",
    unchanged: "Belum ada yang diubah.",
  },

  // ---- activity feed -------------------------------------------------------------------------
  feed: {
    title: "Aktivitas",
    label: "Aktivitas terbaru",
    countLabel: "jumlah aktivitas",
    listLabel: "5 aktivitas terakhir, terbaru di atas",
    empty: "Belum ada aktivitas.",
    office: "Kantor",
    agent: "Staf",
  },

  // ---- policy dialog -------------------------------------------------------------------------
  policy: {
    title: "Atur aturan belanja",
    intro: "Staf tidak bisa membelanjakan uang sebelum kamu mengatur batas ini. Kamu bisa mengubahnya nanti.",
    limitLabel: "Batas belanja harian (Rupiah)",
    blockedLabel: "Aksi yang diblokir",
    blockedHelp: "Staf tidak akan pernah melakukan aksi yang kamu centang.",
    cancel: "Batal",
    save: "Simpan",
    saveAndStart: "Simpan dan mulai",
    saving: "Menyimpan\u2026",
    zeroLimit: "Rp 0: staf tidak bisa membelanjakan apa pun.",
    saveFailed: "Tidak bisa menyimpan aturan. {error}",
    limitEmpty: "Isi batas belanja harian.",
    limitFormat: "Pakai angka saja, misalnya 100000 atau Rp 100.000.",
    limitTooBig: "Batas tidak boleh lebih dari {max}.",
    unknownKind: "Jenis aksi tidak dikenal.",
  },
  actionKinds: {
    read: { label: "Baca data", hint: "Melihat penjualan, stok, dan laporan." },
    draft: { label: "Tulis draf", hint: "Menulis draf dengan model AI." },
    spend: { label: "Belanja", hint: "Memesan stok dan pengeluaran lain." },
    customer_message: {
      label: "Pesan ke pelanggan",
      hint: "Menyiapkan pesan untuk pelanggan (belum dikirim di versi ini).",
    },
  },

  // ---- time and money ----------------------------------------------------------------------------
  time: { zone: "WIB" },

  // ---- errors --------------------------------------------------------------------------------
  errors: {
    generic: "Terjadi kesalahan. Coba lagi.",
    invalidInput: "Isian belum benar.",
    timeout: "Server tidak menjawab tepat waktu.",
    unreachable: "Tidak bisa menghubungi server.",
    unreadable: "Server mengirim jawaban yang tidak terbaca.",
    statusCode: "Server menjawab {status}.",
  },
  // Fixed English messages of the backend API, shown in Indonesian.
  serverMessages: {
    "agent not found": "Staf tidak ditemukan.",
    "business not found": "Usaha tidak ditemukan.",
    "give a name, a role, or both": "Isi nama, peran, atau keduanya.",
    "database unavailable, try again": "Database sedang tidak tersedia. Coba lagi.",
    "this app cannot create a business": "Aplikasi ini tidak bisa membuat usaha.",
    "must not be blank": "Tidak boleh kosong.",
    "unknown action type": "Jenis aksi tidak dikenal.",
    "invalid business name": "Nama usaha tidak valid.",
    "unknown business type": "Jenis usaha tidak dikenal.",
  },
});

/** Fill {name} placeholders. A placeholder without a value stays visible, so a bug shows up in tests. */
export function fmt(template, values = {}) {
  return String(template).replace(/\{(\w+)\}/g, (whole, key) => (key in values ? String(values[key]) : whole));
}

/** All leaf strings of a dictionary, with their path, for tests. */
export function* walk(node = TEXT, path = "") {
  for (const [key, value] of Object.entries(node)) {
    const here = path ? `${path}.${key}` : key;
    if (typeof value === "string") yield [here, value];
    else if (value && typeof value === "object") yield* walk(value, here);
  }
}
