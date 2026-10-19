//! Journal des événements, affiché dans l'appli et recopié sur la sortie d'erreur.

use std::collections::VecDeque;
use std::io::Write;
use std::sync::Mutex;

const KEEP: usize = 200;

#[derive(Default)]
pub struct Journal {
    lines: Mutex<VecDeque<String>>,
}

impl Journal {
    pub fn push(&self, line: impl Into<String>) {
        let line = format!("{}  {}", chrono::Local::now().format("%H:%M:%S"), line.into());
        // Pas d'eprintln! : il panique si la sortie d'erreur est fermée (appli lancée depuis un terminal refermé).
        let _ = writeln!(std::io::stderr(), "{line}");
        let mut lines = self.lines.lock().unwrap();
        if lines.len() == KEEP {
            lines.pop_front();
        }
        lines.push_back(line);
    }

    pub fn lines(&self) -> Vec<String> {
        self.lines.lock().unwrap().iter().cloned().collect()
    }
}
