/*
 * msimg32.dll 代理 —— 让 ANSI 版 HSP 引擎（HSP3 非 Unicode 版）显示简体中文
 *
 * 背景（2026-11-04 在《八星帝》rai8.exe 上实测得出，不是推测）：
 *
 *   1. 游戏的文本**完全不经过 MultiByteToWideChar**。日志确认：整个启动过程中
 *      没有任何 cp=932 的调用，只有 cp=0 的路径处理。所以想靠改 MB2WC 的
 *      代码页来「翻译」是死路。
 *
 *   2. 文件里的 Shift-JIS 字节被**原样**交给 TabbedTextOutA。实测抓到：
 *        91 80 8d ec 90 e0 96 be  ->  SJIS 解码 = 操作説明
 *                                   ->  GBK  解码 = 憖嶌愢柧
 *      屏幕上显示的是「操作説明」，说明 GDI 按 SJIS 解释了这串字节。
 *
 *   3. 反直觉的一点：字体是 FixedSys + DEFAULT_CHARSET(1)。按文档
 *      DEFAULT_CHARSET 应该走系统 ACP(936)，但实测确实走的是 932。
 *      把 charset 改成 GB2312_CHARSET(134) 也无效（画面一个字都没变）。
 *      所以「改字体字符集」这条路同样不通。
 *
 * 解法 —— 不跟「按哪个代码页解释字节」死磕，绕过去：
 *
 *   钩住 TabbedTextOutA，把字节按内容判断编码后**转成宽字符**，
 *   改调 TabbedTextOutW。宽字符由 GDI 直接渲染，与代码页彻底无关。
 *   - 像 GBK 中文的 -> 按 936 解
 *   - 其余的        -> 按 932 解（保持原日文正确）
 *   于是「已汉化的中文行」和「还没翻的日文行」能同时正确显示。
 *
 *   字体同时从 FixedSys 换成 SimSun：FixedSys 没有中文字形，
 *   宋体同时含中日文字形，两边都不缺字。
 *
 * 为什么用 msimg32 而不是 winmm 做代理（踩过的坑）：
 *   rai8.exe 从 winmm 只静态导入 6 个函数，但 HSP 会运行时
 *   GetProcAddress("PlaySound")。代理里没这个导出就返回 NULL，
 *   HSP 立刻弹「#Error 38 Internal Error」。msimg32 只被静态导入
 *   GradientFill 一个，没有代码会动态取它的其它导出。
 *
 * 构建（32 位目标必须用 32 位编译器，否则游戏静默不启动）：
 *   i686-w64-mingw32-gcc -shared -O2 -static-libgcc \
 *       -o msimg32.dll msimg32_proxy.c -Wl,--kill-at
 *
 * 用法：把 msimg32.dll 放进游戏目录（与 exe 同级）。游戏加载时优先用本地的。
 */
#include <windows.h>

static HMODULE g_real;
static volatile LONG g_real_lock;

typedef LONG (WINAPI *PFN_TTO)(HDC, int, int, LPCSTR, int, int, const INT *, int);
typedef LONG (WINAPI *PFN_TTOW)(HDC, int, int, LPCWSTR, int, int, const INT *, int);
typedef HFONT (WINAPI *PFN_CFI)(const LOGFONTA *);

static PFN_TTO g_real_tto;
static PFN_TTOW g_real_ttow;
static PFN_CFI g_real_cfi;

/* 取真正的系统 DLL —— 必须全路径，否则又拿到自己 */
static HMODULE real_dll(const char *name)
{
    if (g_real) return g_real;
    if (InterlockedCompareExchange(&g_real_lock, 1, 0) == 0) {
        char path[MAX_PATH];
        UINT n = GetSystemDirectoryA(path, MAX_PATH - 20);
        if (n && n < MAX_PATH - 20) {
            lstrcatA(path, "\\");
            lstrcatA(path, name);
            g_real = LoadLibraryA(path);
        }
    }
    return g_real;
}

/*
 * 这串字节像 GBK 中文吗？
 *
 * 宁可漏判（当成日文）也不误判（当成中文）：误判会让原本正常的日文
 * 变成错字，漏判只是「这条还是日文」，对汉化流程无害。
 *
 *  1. 出现平/片假名区字节对（SJIS）    -> 日文
 *  2. 出现 SJIS 汉字区首字节 0x88-0x9F -> 日文
 *  3. 必须真含 GBK 汉字（0xB0-0xF7 + 0xA1-0xFE）才判中文
 *
 * 已知局限：纯汉字且汉字全落在 0xE0-0xEF 的日文短串会被判成中文，
 * 显示为错字。这类串在台词里极少，且汉化后本来就要换成中文。
 */
static int looks_gbk_cn(const unsigned char *s, int n)
{
    int i, gbk_han = 0;

    for (i = 0; i + 1 < n; i++) {
        if (s[i] == 0x82 && s[i + 1] >= 0x9F && s[i + 1] <= 0xF1) return 0;
        if (s[i] == 0x83 && s[i + 1] >= 0x40 && s[i + 1] <= 0x96) return 0;
        if (s[i] >= 0x88 && s[i] <= 0x9F) return 0;
        if (s[i] >= 0xB0 && s[i] <= 0xF7 && s[i + 1] >= 0xA1 && s[i + 1] <= 0xFE)
            gbk_han = 1;
    }
    return gbk_han;
}

/* 一律走 W 版：编码问题在这里被彻底消解 */
static LONG WINAPI Hook_TTO(HDC hdc, int x, int y, LPCSTR s, int n,
                            int ntab, const INT *tabs, int origin)
{
    int len;

    if (s && g_real_ttow) {
        UINT cp;
        int wn;

        len = (n < 0) ? lstrlenA(s) : n;
        if (len > 0) {
            cp = looks_gbk_cn((const unsigned char *)s, len) ? 936 : 932;
            wn = MultiByteToWideChar(cp, 0, s, len, NULL, 0);
            if (wn > 0) {
                WCHAR *wb = (WCHAR *)HeapAlloc(GetProcessHeap(), 0,
                                               (SIZE_T)wn * sizeof(WCHAR));
                if (wb) {
                    LONG r;
                    MultiByteToWideChar(cp, 0, s, len, wb, wn);
                    r = g_real_ttow(hdc, x, y, wb, wn, ntab, tabs, origin);
                    HeapFree(GetProcessHeap(), 0, wb);
                    return r;
                }
            }
        }
    }
    return g_real_tto ? g_real_tto(hdc, x, y, s, n, ntab, tabs, origin) : 0;
}

/* FixedSys 没有中文字形；宋体中日文都覆盖 */
static HFONT WINAPI Hook_CFI(const LOGFONTA *lf)
{
    LOGFONTA copy;

    if (!g_real_cfi) return NULL;
    if (!lf) return g_real_cfi(lf);

    copy = *lf;
    copy.lfCharSet = GB2312_CHARSET;
    if (_stricmp(lf->lfFaceName, "FixedSys") == 0)
        lstrcpyA(copy.lfFaceName, "SimSun");
    return g_real_cfi(&copy);
}

/* 只改主模块的导入表 */
static int patch_iat(HMODULE mod)
{
    IMAGE_DOS_HEADER *dos;
    IMAGE_NT_HEADERS *nt;
    IMAGE_DATA_DIRECTORY dir;
    IMAGE_IMPORT_DESCRIPTOR *desc;
    int patched = 0;

    if (!mod) return 0;
    dos = (IMAGE_DOS_HEADER *)mod;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    nt = (IMAGE_NT_HEADERS *)((BYTE *)mod + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return 0;

    dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress) return 0;

    desc = (IMAGE_IMPORT_DESCRIPTOR *)((BYTE *)mod + dir.VirtualAddress);
    for (; desc->Name; desc++) {
        const char *dll = (const char *)((BYTE *)mod + desc->Name);
        IMAGE_THUNK_DATA *thunk, *iat;

        if (_stricmp(dll, "USER32.dll") != 0 && _stricmp(dll, "GDI32.dll") != 0)
            continue;

        thunk = (IMAGE_THUNK_DATA *)((BYTE *)mod +
                (desc->OriginalFirstThunk ? desc->OriginalFirstThunk
                                          : desc->FirstThunk));
        iat = (IMAGE_THUNK_DATA *)((BYTE *)mod + desc->FirstThunk);

        for (; thunk->u1.Function; thunk++, iat++) {
            IMAGE_IMPORT_BY_NAME *ibn;
            const char *nm;
            ULONG_PTR hook;
            DWORD old;

            if (thunk->u1.Ordinal & IMAGE_ORDINAL_FLAG) continue;
            ibn = (IMAGE_IMPORT_BY_NAME *)((BYTE *)mod + thunk->u1.AddressOfData);
            nm = (const char *)ibn->Name;

            if (strcmp(nm, "TabbedTextOutA") == 0)
                hook = (ULONG_PTR)Hook_TTO;
            else if (strcmp(nm, "CreateFontIndirectA") == 0)
                hook = (ULONG_PTR)Hook_CFI;
            else
                continue;

            if (VirtualProtect(&iat->u1.Function, sizeof(void *),
                               PAGE_READWRITE, &old)) {
                iat->u1.Function = hook;
                VirtualProtect(&iat->u1.Function, sizeof(void *), old, &old);
                patched++;
            }
        }
    }
    return patched;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason != DLL_PROCESS_ATTACH) return TRUE;
    DisableThreadLibraryCalls(inst);

    /*
     * DllMain 里绝不做 LoadLibrary —— 加载器锁未释放，会死锁。
     * 这里只取已加载模块里的函数地址，安全。
     */
    g_real_tto = (PFN_TTO)GetProcAddress(GetModuleHandleA("user32.dll"),
                                         "TabbedTextOutA");
    g_real_ttow = (PFN_TTOW)GetProcAddress(GetModuleHandleA("user32.dll"),
                                           "TabbedTextOutW");
    g_real_cfi = (PFN_CFI)GetProcAddress(GetModuleHandleA("gdi32.dll"),
                                         "CreateFontIndirectA");

    if (g_real_tto && g_real_ttow)
        patch_iat(GetModuleHandleA(NULL));
    return TRUE;
}

/* ---- msimg32 的三个导出，原样转发 ---- */

static FARPROC real(const char *name)
{
    HMODULE m = real_dll("msimg32.dll");
    return m ? GetProcAddress(m, name) : NULL;
}

__declspec(dllexport) BOOL WINAPI GradientFill(HDC hdc, PTRIVERTEX v, ULONG nv,
                                               PVOID m, ULONG nm, ULONG mode)
{
    typedef BOOL (WINAPI *F)(HDC, PTRIVERTEX, ULONG, PVOID, ULONG, ULONG);
    F f = (F)real("GradientFill");
    return f ? f(hdc, v, nv, m, nm, mode) : FALSE;
}

__declspec(dllexport) BOOL WINAPI TransparentBlt(HDC d, int x, int y, int w,
                                                 int h, HDC s, int sx, int sy,
                                                 int sw, int sh, UINT key)
{
    typedef BOOL (WINAPI *F)(HDC, int, int, int, int, HDC, int, int, int, int, UINT);
    F f = (F)real("TransparentBlt");
    return f ? f(d, x, y, w, h, s, sx, sy, sw, sh, key) : FALSE;
}

__declspec(dllexport) BOOL WINAPI AlphaBlend(HDC d, int x, int y, int w, int h,
                                             HDC s, int sx, int sy, int sw,
                                             int sh, BLENDFUNCTION bf)
{
    typedef BOOL (WINAPI *F)(HDC, int, int, int, int, HDC, int, int, int, int, BLENDFUNCTION);
    F f = (F)real("AlphaBlend");
    return f ? f(d, x, y, w, h, s, sx, sy, sw, sh, bf) : FALSE;
}
