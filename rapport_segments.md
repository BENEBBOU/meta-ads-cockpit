# Segment performance report

Conversion metric: `initiate_checkout` · FDR target: 0.05 · bootstrap: 2000 resamples

## age_gender — Audience demographics

Account rate: **0.2101 conv/$** (8,987 conversions over 42,782 $) · dispersion phi = 70.4 · 12 segments tested
_9 segment(s) too small to test (227 $ of spend)._

```
       segment  spend   conv  cost/conv    CI 95%  index        q sig
35-44 / female 4380.0 1287.0       3.40 2.97–3.87   1.40 0.000857 yes
25-34 / female 8060.0 2366.0       3.41 3.10–3.76   1.40 0.000857 yes
45-54 / female 1591.0  342.0       4.65 3.96–5.62   1.02    0.831    
  35-44 / male 6049.0 1237.0       4.89 4.30–5.65   0.97    0.648    
18-24 / female 3832.0  754.0       5.08 4.48–5.84   0.94    0.311    
   ...
  65+ / female  413.0  54.0  7.65  5.54–11.79 0.62   0.0015 yes
  18-24 / male 4400.0 559.0  7.87   6.92–9.09 0.60 0.000857 yes
55-64 / female  578.0  70.0  8.25  6.19–11.63 0.58 0.000857 yes
  55-64 / male  914.0 108.0  8.46  6.56–11.32 0.56 0.000857 yes
    65+ / male  727.0  35.0 20.76 14.32–34.06 0.23 0.000857 yes
```

**Reallocation.** Moving 9,381 $ out of 6 under-performing segment(s) into 2 winner(s):

- linear ceiling: 10,523 conversions (+1,570, +17.5%)
- with saturation (alpha=0.8): 9,842 (+889, +9.9%)

Largest donors:
```
     segment   spend  conversions  cost_per_conv
18-24 / male 4399.85        559.0           7.87
45-54 / male 2350.27        359.0           6.55
55-64 / male  913.95        108.0           8.46
```

## device — Device class

Account rate: **0.2093 conv/$** (8,956 conversions over 42,785 $) · dispersion phi = 127.3 · 4 segments tested
_2 segment(s) too small to test (45 $ of spend)._

```
           segment   spend   conv  cost/conv     CI 95%  index        q sig
            iphone 20168.0 5139.0       3.92  3.58–4.30   1.22 0.000666 yes
           desktop   254.0   46.0       5.52  3.96–8.49   0.86    0.432    
android_smartphone 22181.0 3756.0       5.91  5.21–6.70   0.81 0.000666 yes
    android_tablet   137.0   10.0      13.73 7.93–32.11   0.35 0.000666 yes
```

**Reallocation.** Moving 22,318 $ out of 2 under-performing segment(s) into 1 winner(s):

- linear ceiling: 10,872 conversions (+1,921, +21.5%)
- with saturation (alpha=0.8): 9,373 (+422, +4.7%)

Largest donors:
```
           segment    spend  conversions  cost_per_conv
android_smartphone 22181.05       3756.0           5.91
    android_tablet   137.33         10.0          13.73
```

## hourly — Hour of day

Account rate: **0.1974 conv/$** (8,394 conversions over 42,532 $) · dispersion phi = 7.6 · 24 segments tested

```
            segment  spend  conv  cost/conv    CI 95%  index     q sig
14:00:00 - 14:59:59 2044.0 481.0       4.25 3.52–5.23   1.19 0.208    
16:00:00 - 16:59:59 1991.0 461.0       4.32 3.62–5.27   1.17 0.252    
15:00:00 - 15:59:59 1971.0 455.0       4.33 3.74–5.05   1.17 0.093    
20:00:00 - 20:59:59 2206.0 481.0       4.59 3.86–5.52   1.10 0.391    
21:00:00 - 21:59:59 2501.0 544.0       4.60 3.93–5.46   1.10 0.391    
   ...
03:00:00 - 03:59:59 749.0 106.0  7.07  5.79–8.79 0.72 0.006 yes
04:00:00 - 04:59:59 483.0  65.0  7.43  5.85–9.89 0.68 0.004 yes
06:00:00 - 06:59:59 338.0  44.0  7.67 5.50–11.79 0.66  0.06    
07:00:00 - 07:59:59 455.0  47.0  9.68 7.18–13.91 0.52 0.004 yes
05:00:00 - 05:59:59 383.0  36.0 10.64 7.62–16.43 0.48 0.004 yes
```

_No statistically separated donor/recipient pair — nothing to reallocate._

## placement — Where the ad is shown

Account rate: **0.2093 conv/$** (8,956 conversions over 42,785 $) · dispersion phi = 144.5 · 9 segments tested
_15 segment(s) too small to test (206 $ of spend)._

```
                      segment   spend   conv  cost/conv    CI 95%  index       q sig
             instagram / feed  8157.0 2298.0       3.55 3.23–3.91   1.35 0.00075 yes
              facebook / feed  6879.0 1817.0       3.79 3.20–4.50   1.26 0.00075 yes
instagram / instagram_stories  5628.0 1432.0       3.93 3.50–4.46   1.22 0.00129 yes
  instagram / instagram_reels 10589.0 2181.0       4.85 4.44–5.32   0.98   0.642    
  facebook / facebook_stories   724.0  129.0       5.61 4.62–7.05   0.85   0.111    
```

**Reallocation.** Moving 10,603 $ out of 4 under-performing segment(s) into 3 winner(s):

- linear ceiling: 10,703 conversions (+1,814, +20.4%)
- with saturation (alpha=0.8): 10,036 (+1,147, +12.9%)

Largest donors:
```
                      segment   spend  conversions  cost_per_conv
    facebook / facebook_reels 6433.64        881.0           7.30
audience_network / an_classic 3107.05         53.0          58.62
    facebook / instream_video  614.05         85.0           7.22
```

## region — Geography (link clicks — pixel conversions unavailable on this breakdown)

Account rate: **22.0465 conv/$** (943,275 conversions over 42,786 $) · dispersion phi = 1009.4 · 9 segments tested
_13 segment(s) too small to test (100 $ of spend)._

```
                 segment   spend     conv  cost/conv    CI 95%  index     q sig
Gharb-Chrarda-Béni Hssen  1133.0  29873.0       0.04 0.03–0.05   1.20 0.211    
        Souss-Massa-Drâa  5952.0 150831.0       0.04 0.04–0.04   1.15 0.018 yes
           Fès-Boulemane   401.0   9790.0       0.04 0.03–0.05   1.11 0.211    
 Rabat-Salé-Zemmour-Zaer  5183.0 118873.0       0.04 0.04–0.05   1.04 0.618    
         Tangier-Tetouan 10927.0 246347.0       0.04 0.04–0.05   1.02 0.503    
```

_Ranking only: this breakdown exposes no pixel conversion, so the outcome is a proxy and no budget recommendation is derived from it._
