//! Conversion des images décodées (I420, BT.709 plage limitée) en RGBA à demi-résolution pour l'aperçu.

/// Taille en octets d'une image I420 `width`×`height`.
pub fn i420_len(width: usize, height: usize) -> usize {
    width * height * 3 / 2
}

/// Image I420 pleine taille -> RGBA à demi-résolution (un pixel sur deux, la chrominance tombe juste).
pub fn i420_to_rgba_half(frame: &[u8], width: usize, height: usize) -> Vec<u8> {
    let (w, h) = (width / 2, height / 2);
    let (y_plane, chroma) = frame.split_at(width * height);
    let (u_plane, v_plane) = chroma.split_at(w * h);
    let mut out = Vec::with_capacity(w * h * 4);
    for row in 0..h {
        for col in 0..w {
            let y = (y_plane[2 * row * width + 2 * col] as i32 - 16) * 298;
            let u = u_plane[row * w + col] as i32 - 128;
            let v = v_plane[row * w + col] as i32 - 128;
            // BT.709 plage limitée, coefficients ×256.
            let r = (y + 459 * v + 128) >> 8;
            let g = (y - 55 * u - 136 * v + 128) >> 8;
            let b = (y + 541 * u + 128) >> 8;
            out.extend([r.clamp(0, 255) as u8, g.clamp(0, 255) as u8, b.clamp(0, 255) as u8, 255]);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(width: usize, height: usize, yuv: impl Fn(usize, usize) -> (u8, u8, u8)) -> Vec<u8> {
        let mut f = vec![0u8; i420_len(width, height)];
        let (cw, ch) = (width / 2, height / 2);
        for y in 0..height {
            for x in 0..width {
                f[y * width + x] = yuv(x, y).0;
            }
        }
        for y in 0..ch {
            for x in 0..cw {
                let (_, u, v) = yuv(2 * x, 2 * y);
                f[width * height + y * cw + x] = u;
                f[width * height + cw * ch + y * cw + x] = v;
            }
        }
        f
    }

    fn close(px: &[u8], want: [u8; 4]) -> bool {
        px.iter().zip(want).all(|(&a, b)| (a as i32 - b as i32).abs() <= 2)
    }

    #[test]
    fn blanc_noir_et_rouge() {
        // Quand l'image est blanche (Y=235), noire (Y=16) ou rouge BT.709, les pixels RGBA doivent suivre.
        for (yuv, rgba) in [((235, 128, 128), [255, 255, 255, 255]), ((16, 128, 128), [0, 0, 0, 255]), ((63, 102, 240), [255, 0, 0, 255])] {
            let out = i420_to_rgba_half(&frame(8, 4, |_, _| yuv), 8, 4);
            assert_eq!(out.len(), 4 * 2 * 4);
            assert!(out.chunks(4).all(|px| close(px, rgba)), "{yuv:?} -> {:?}", &out[..4]);
        }
    }

    #[test]
    fn garde_la_disposition_de_l_image() {
        // Quand la moitié gauche est blanche et la droite noire, l'aperçu doit garder cette disposition.
        let out = i420_to_rgba_half(&frame(8, 4, |x, _| if x < 4 { (235, 128, 128) } else { (16, 128, 128) }), 8, 4);
        assert_eq!(out.len(), 4 * 2 * 4);
        for row in out.chunks(4 * 4) {
            assert!(close(&row[0..4], [255, 255, 255, 255]) && close(&row[4..8], [255, 255, 255, 255]));
            assert!(close(&row[8..12], [0, 0, 0, 255]) && close(&row[12..16], [0, 0, 0, 255]));
        }
    }
}
