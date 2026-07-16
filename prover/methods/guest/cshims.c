#include <stddef.h>
int strcmp(const char*a,const char*b){while(*a&&*a==*b){a++;b++;}return (unsigned char)*a-(unsigned char)*b;}
char* strchr(const char*s,int c){while(*s){if(*s==(char)c)return (char*)s;s++;}return c?(char*)0:(char*)s;}
unsigned long strtoul(const char*s,char**e,int b){(void)b;unsigned long r=0;while(*s>='0'&&*s<='9'){r=r*10+(unsigned long)(*s-'0');s++;}if(e)*e=(char*)s;return r;}
char* getenv(const char*n){(void)n;return (char*)0;}
/* static heap for newlib malloc's _sbrk (guest has no OS) */
static char _heap[1<<20];
static char* _hp = _heap;
void* _sbrk(int incr){ char* p=_hp; if(_hp+incr > _heap+sizeof(_heap)) return (void*)-1; _hp+=incr; return p; }
